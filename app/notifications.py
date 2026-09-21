"""
Уведомления о событиях сайта по подписке пользователя (настройки — см.
app/cabinet.py: notification_settings, шаблон cabinet/profile.html). Один
канал доставки на человека (User.notify_channel) и отдельные подписки на
события (User.notify_charge/notify_payment/notify_news/notify_forum/notify_board_chat).

Три реально работающих канала:
  - EMAIL — через уже существующий ящик правления (MailboxSettings/
    app/mail_client.py, тот же, что и у /mailbox/), готовность — заполненный
    Person.email.
  - TELEGRAM — через бота (TelegramSettings/app/telegram_bot.py),
    готовность — Person.telegram_chat_id (привязка через /start, см.
    telegram_bot.py, НЕ то же самое, что свободный текст Person.telegram).
  - WEBPUSH — push-уведомления браузера (WebPushSettings/app/webpush.py),
    готовность — хотя бы одна WebPushSubscription (в отличие от email/
    telegram, подписок может быть несколько — разные браузеры/устройства,
    рассылается во все разом, а не в одну).

VK/MAX убраны из списка каналов (по решению пользователя) — Bot API VK
не позволяет писать первым произвольным пользователям без их явного
опт-ина через сообщество, а готового сообщества с такой настройкой нет;
делать половинчатую интеграцию не стали.
"""
import datetime as dt

from flask import current_app

from . import database
from .auth import ROLE_LEVEL
from .i18n import translate as _
from .mail_client import MailError, send_message
from .models import (
    BoardChatMessage, MailboxSettings, NotificationChannel, RoleEnum, TelegramSettings, WebPushSettings,
    WebPushSubscription, User,
)
from . import telegram_bot
from . import webpush

BOARD_CHAT_UNREAD_THRESHOLD = dt.timedelta(minutes=10)


def user_for_person(person_id: int | None) -> User | None:
    """Учётная запись, привязанная к этому человеку — Person не хранит
    обратной ссылки на User (relationship объявлена только от User),
    поэтому ищем по person_id напрямую. None, если у человека нет
    учётной записи для входа (тогда и уведомлять некого)."""
    if person_id is None:
        return None
    return database.db_session.query(User).filter_by(person_id=person_id).first()


def channel_is_ready(user: User, channel: NotificationChannel) -> bool:
    """Готов ли канал к реальной отправке (используется и при сохранении
    формы настроек — нельзя включить неподтверждённый канал, и здесь же,
    при отправке — контакт/привязка могли быть сброшены позже)."""
    if user is None or user.person is None:
        return False
    if channel == NotificationChannel.EMAIL:
        return bool(user.person.email)
    if channel == NotificationChannel.TELEGRAM:
        return bool(user.person.telegram_chat_id)
    if channel == NotificationChannel.WEBPUSH:
        return database.db_session.query(WebPushSubscription).filter_by(user_id=user.id).first() is not None
    return False


def notify_subscribers(event: str, subject: str, body_text: str, exclude_user_id: int | None = None) -> None:
    """Рассылка всем, кто подписан на это событие (notify_<event>=True) —
    новости/объявления доски (всем) и новая тема форума (всем, кроме
    автора). Кооператив небольшой — цикл по всем подписчикам, без
    отдельной оптимизации на массовую рассылку."""
    column = getattr(User, f"notify_{event}")
    users = database.db_session.query(User).filter(column.is_(True)).all()
    for user in users:
        if exclude_user_id is not None and user.id == exclude_user_id:
            continue
        notify(user, event, subject, body_text)


def notify_users(user_ids, event: str, subject: str, body_text: str, exclude_user_id: int | None = None) -> None:
    """Как notify_subscribers, но только для конкретного набора user_id —
    участники темы форума при ответе (в отличие от новой темы, которая
    уходит ВСЕМ подписчикам)."""
    ids = {uid for uid in user_ids if uid is not None and uid != exclude_user_id}
    if not ids:
        return
    column = getattr(User, f"notify_{event}")
    users = database.db_session.query(User).filter(User.id.in_(ids), column.is_(True)).all()
    for user in users:
        notify(user, event, subject, body_text)


def notify(user: User | None, event: str, subject: str, body_text: str) -> None:
    """event — один из "charge"/"payment"/"news"/"forum"/"board_chat",
    соответствует колонке notify_<event> на User. Ничего не делает, если
    событие/канал не включены подпиской или канал не готов к отправке —
    вызывающему коду не нужно проверять это самому. Ошибка отправки
    логируется и не пробрасывается — уведомление не должно ронять
    транзакцию, породившую событие (начисление, платёж и т.п. к этому
    моменту уже сохранены)."""
    if user is None or user.notify_channel is None:
        return
    if not getattr(user, f"notify_{event}", False):
        return
    if not channel_is_ready(user, user.notify_channel):
        return

    if user.notify_channel == NotificationChannel.EMAIL:
        settings = database.db_session.query(MailboxSettings).first()
        if settings is None:
            return
        try:
            send_message(settings, [user.person.email], subject, body_text)
        except MailError:
            current_app.logger.exception(
                "Не удалось отправить email-уведомление user_id=%s событие=%s", user.id, event,
            )
    elif user.notify_channel == NotificationChannel.TELEGRAM:
        settings = database.db_session.query(TelegramSettings).first()
        if settings is None or not telegram_bot.is_configured(settings):
            return
        try:
            telegram_bot.send_message(settings, user.person.telegram_chat_id, f"{subject}\n\n{body_text}")
        except telegram_bot.TelegramError:
            current_app.logger.exception(
                "Не удалось отправить telegram-уведомление user_id=%s событие=%s", user.id, event,
            )
    elif user.notify_channel == NotificationChannel.WEBPUSH:
        settings = database.db_session.query(WebPushSettings).first()
        if settings is None or not settings.public_key:
            return
        subscriptions = database.db_session.query(WebPushSubscription).filter_by(user_id=user.id).all()
        for subscription in subscriptions:
            try:
                webpush.send(settings, subscription, subject, body_text)
            except webpush.WebPushError as exc:
                if webpush.is_expired(exc):
                    database.db_session.delete(subscription)
                    database.db_session.commit()
                else:
                    current_app.logger.exception(
                        "Не удалось отправить webpush-уведомление user_id=%s событие=%s", user.id, event,
                    )


def send_test_notification(user: User) -> tuple[bool, str]:
    """Тестовая отправка по кнопке «Проверить уведомления» (cabinet/profile.html) —
    тем же путём, что и notify(), но по ЯВНОМУ действию пользователя, а не
    событию сайта: без проверки notify_<event> (тестируем сам канал, а не
    подписку на конкретное событие) и с пробросом результата вызывающему
    коду (notify() ошибки только логирует и работает молча — тут наоборот,
    ошибку нужно показать самому пользователю, зачем и звали кнопку).
    Возвращает (успех, текст для flash)."""
    if user is None or user.notify_channel is None:
        return False, _("Сначала выберите канал уведомлений и сохраните настройки.")
    if not channel_is_ready(user, user.notify_channel):
        return False, _("Чтобы получать уведомления этим способом, сначала укажите и сохраните соответствующий контакт в профиле.")

    subject = _("Тестовое уведомление")
    body_text = _("Это тестовое уведомление от системы учёта кооператива — если оно дошло, значит канал настроен верно.")

    try:
        if user.notify_channel == NotificationChannel.EMAIL:
            settings = database.db_session.query(MailboxSettings).first()
            if settings is None:
                return False, _("Почтовый ящик кооператива ещё не настроен — обратитесь в правление.")
            send_message(settings, [user.person.email], subject, body_text)
        elif user.notify_channel == NotificationChannel.TELEGRAM:
            settings = database.db_session.query(TelegramSettings).first()
            if settings is None or not telegram_bot.is_configured(settings):
                return False, _("Telegram-бот кооператива ещё не настроен — обратитесь в правление.")
            telegram_bot.send_message(settings, user.person.telegram_chat_id, f"{subject}\n\n{body_text}")
        elif user.notify_channel == NotificationChannel.WEBPUSH:
            settings = database.db_session.query(WebPushSettings).first()
            if settings is None or not settings.public_key:
                return False, _("Push-уведомления кооператива ещё не настроены — обратитесь в правление.")
            subscriptions = database.db_session.query(WebPushSubscription).filter_by(user_id=user.id).all()
            if not subscriptions:
                return False, _("Нет ни одной подписки браузера на push-уведомления — нажмите «Подписаться на push-уведомления» ниже.")
            sent = 0
            for subscription in subscriptions:
                try:
                    webpush.send(settings, subscription, subject, body_text)
                    sent += 1
                except webpush.WebPushError as exc:
                    if webpush.is_expired(exc):
                        database.db_session.delete(subscription)
                        database.db_session.commit()
                    else:
                        raise
            if sent == 0:
                return False, _("Не удалось отправить ни на одно из подписанных устройств — возможно, подписки устарели.")
    except MailError as exc:
        return False, _("Не удалось отправить письмо: {error}", error=str(exc))
    except telegram_bot.TelegramError as exc:
        return False, _("Не удалось отправить сообщение в Telegram: {error}", error=str(exc))
    except webpush.WebPushError as exc:
        return False, _("Не удалось отправить push-уведомление: {error}", error=str(exc))

    return True, _("Тестовое уведомление отправлено — проверьте, что оно дошло.")


def run_board_chat_digest() -> int:
    """Уведомление членам правления о непрочитанных сообщениях в чате
    правления — см. scripts/board_chat_digest.py (тонкая cron-обёртка,
    сама логика здесь, тот же принцип, что у app/penalty.py:accrue_penalties
    и её cron-обёртки scripts/accrue_penalty.py). Возвращает число
    отправленных уведомлений.

    User.board_chat_notified_message_id хранит id последнего сообщения, о
    непрочтении которого уже уведомили — без этого повторный запуск (раз в
    5 минут, см. .sh-обёртку) слал бы письмо заново на каждый прогон, пока
    сообщение остаётся непрочитанным."""
    last_message = (
        database.db_session.query(BoardChatMessage)
        .order_by(BoardChatMessage.id.desc())
        .first()
    )
    if last_message is None:
        return 0
    if dt.datetime.utcnow() - last_message.created_at < BOARD_CHAT_UNREAD_THRESHOLD:
        return 0

    notified = 0
    for user in database.db_session.query(User).filter_by(notify_board_chat=True):
        if ROLE_LEVEL[user.role] < ROLE_LEVEL[RoleEnum.BOARD]:
            continue
        if user.board_chat_read_at and user.board_chat_read_at >= last_message.created_at:
            continue
        if user.board_chat_notified_message_id == last_message.id:
            continue
        notify(
            user, "board_chat", "Непрочитанные сообщения в чате правления",
            "В чате правления есть непрочитанные сообщения (более 10 минут).",
        )
        user.board_chat_notified_message_id = last_message.id
        notified += 1
    database.db_session.commit()
    return notified
