"""
Уведомления о событиях сайта по подписке пользователя (настройки — см.
app/cabinet.py: profile, шаблон cabinet/profile.html). Один канал доставки
на человека (User.notify_channel) и отдельные подписки на события
(User.notify_charge/notify_payment/notify_news/notify_forum/notify_board_chat).

Реально отправляется сейчас только EMAIL — через уже существующий ящик
правления (MailboxSettings/app/mail_client.py, тот же, что и у /mailbox/).
Telegram/VK/MAX можно выбрать в настройках (проверяется, что
соответствующее поле контакта в профиле заполнено), но notify() для них
ничего не отправляет — задел на будущее, отдельная бот/API-интеграция
здесь не делается.
"""
import datetime as dt

from flask import current_app

from . import database
from .auth import ROLE_LEVEL
from .mail_client import MailError, send_message
from .models import BoardChatMessage, MailboxSettings, NotificationChannel, RoleEnum, User

BOARD_CHAT_UNREAD_THRESHOLD = dt.timedelta(minutes=10)

CHANNEL_CONTACT_FIELD = {
    NotificationChannel.EMAIL: "email",
    NotificationChannel.TELEGRAM: "telegram",
    NotificationChannel.VK: "vk",
    NotificationChannel.MAX: "max_messenger",
}


def user_for_person(person_id: int | None) -> User | None:
    """Учётная запись, привязанная к этому человеку — Person не хранит
    обратной ссылки на User (relationship объявлена только от User),
    поэтому ищем по person_id напрямую. None, если у человека нет
    учётной записи для входа (тогда и уведомлять некого)."""
    if person_id is None:
        return None
    return database.db_session.query(User).filter_by(person_id=person_id).first()


def channel_is_ready(user: User, channel: NotificationChannel) -> bool:
    """Заполнено ли у person поле контакта, соответствующее каналу.
    Используется и при сохранении формы профиля (нельзя включить канал без
    контакта), и здесь же, при отправке (контакт мог быть очищен позже)."""
    if user is None or user.person is None:
        return False
    return bool(getattr(user.person, CHANNEL_CONTACT_FIELD[channel], None))


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
    событие/канал не включены подпиской или контакт для канала не
    заполнен — вызывающему коду не нужно проверять это самому. Ошибка
    отправки (MailError) логируется и не пробрасывается — уведомление не
    должно ронять транзакцию, породившую событие (начисление, платёж и
    т.п. к этому моменту уже сохранены)."""
    if user is None or user.notify_channel is None:
        return
    if not getattr(user, f"notify_{event}", False):
        return
    if not channel_is_ready(user, user.notify_channel):
        return
    if user.notify_channel != NotificationChannel.EMAIL:
        return  # telegram/vk/max — задел на будущее, отправка не реализована

    settings = database.db_session.query(MailboxSettings).first()
    if settings is None:
        return
    try:
        send_message(settings, [user.person.email], subject, body_text)
    except MailError:
        current_app.logger.exception(
            "Не удалось отправить уведомление user_id=%s событие=%s", user.id, event,
        )


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
