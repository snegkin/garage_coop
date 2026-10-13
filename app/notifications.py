"""
Уведомления о событиях сайта по подписке пользователя (настройки — см.
app/cabinet.py: notification_settings, шаблон cabinet/profile.html). Один
канал доставки на человека (User.notify_channel) и отдельные подписки на
события (User.notify_charge/notify_payment/notify_news/notify_forum/
notify_vote/notify_meeting/notify_my_requests/notify_documents/notify_board_chat).
Правлению, кроме того, — уведомления о действиях на сайте по группам
событий журнала аудита (User.notify_board_events, см. раздел
«Уведомления правлению» ниже).

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
import json

from flask import current_app, has_request_context, request

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


def notify_subscribers(event: str, subject: str, body_text: str, exclude_user_id: int | None = None,
                       board_group: str | None = None) -> None:
    """Рассылка всем, кто подписан на это событие (notify_<event>=True) —
    новости/объявления доски (всем) и новая тема форума (всем, кроме
    автора). Кооператив небольшой — цикл по всем подписчикам, без
    отдельной оптимизации на массовую рассылку.

    board_group — группа уведомлений правления, в которую это же событие
    уже уходит через журнал аудита (см. queue_board_event): члены
    правления, подписанные на неё, второе такое же сообщение не получают."""
    column = getattr(User, f"notify_{event}")
    users = database.db_session.query(User).filter(column.is_(True)).all()
    for user in users:
        if exclude_user_id is not None and user.id == exclude_user_id:
            continue
        if board_group is not None and _receives_board_group(user, board_group):
            continue
        notify(user, event, subject, body_text)


def notify_users(user_ids, event: str, subject: str, body_text: str, exclude_user_id: int | None = None,
                 board_group: str | None = None) -> None:
    """Как notify_subscribers, но только для конкретного набора user_id —
    участники темы форума при ответе (в отличие от новой темы, которая
    уходит ВСЕМ подписчикам)."""
    ids = {uid for uid in user_ids if uid is not None and uid != exclude_user_id}
    if not ids:
        return
    column = getattr(User, f"notify_{event}")
    users = database.db_session.query(User).filter(User.id.in_(ids), column.is_(True)).all()
    for user in users:
        if board_group is not None and _receives_board_group(user, board_group):
            continue
        notify(user, event, subject, body_text)


def notify_persons(person_ids, event: str, subject: str, body_text: str, exclude_user_id: int | None = None,
                   board_group: str | None = None) -> None:
    """Как notify_users, но по id людей — учётные записи тех из них, у кого
    они есть (участники голосования считаются по людям-собственникам)."""
    ids = [pid for pid in person_ids if pid is not None]
    if not ids:
        return
    user_ids = [uid for (uid,) in database.db_session.query(User.id).filter(User.person_id.in_(ids))]
    notify_users(user_ids, event, subject, body_text, exclude_user_id=exclude_user_id, board_group=board_group)


def notify(user: User | None, event: str, subject: str, body_text: str) -> None:
    """event — один из "charge"/"payment"/"news"/"forum"/"board_chat"/
    "vote"/"meeting"/"my_requests"/"documents",
    соответствует колонке notify_<event> на User. Ничего не делает, если
    событие/канал не включены подпиской или канал не готов к отправке —
    вызывающему коду не нужно проверять это самому. Ошибка отправки
    логируется и не пробрасывается — уведомление не должно ронять
    транзакцию, породившую событие (начисление, платёж и т.п. к этому
    моменту уже сохранены)."""
    if user is None or not getattr(user, f"notify_{event}", False):
        return
    _deliver(user, event, subject, body_text)


def _deliver(user: User, event: str, subject: str, body_text: str) -> None:
    """Отправка по выбранному каналу без проверки подписки на событие —
    её делает вызывающий код: notify() по колонке notify_<event>,
    _dispatch_board_event() по группе в notify_board_events."""
    if user.notify_channel is None:
        return
    if not channel_is_ready(user, user.notify_channel):
        return
    # Страховка поверх перехвата ошибок конкретных каналов ниже: любое
    # непредвиденное исключение (кривой ключ VAPID давал ValueError из
    # pywebpush) иначе откатывало загрузку выписки целиком.
    try:
        _send(user, event, subject, body_text)
    except Exception:
        current_app.logger.exception(
            "Не удалось отправить уведомление user_id=%s событие=%s", user.id, event,
        )


def _send(user: User, event: str, subject: str, body_text: str) -> None:
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


# ---------------------------------------------------------------------------
# Отправка после коммита
# ---------------------------------------------------------------------------
#
# Уведомление о действии должно уходить только если действие реально
# сохранилось — тот же принцип, что у журнала аудита (см. docstring
# app/audit.py). Поэтому код, порождающий событие посреди транзакции
# (audit.record, resolve_if_due в proposals.py), не отправляет сразу, а
# кладёт отправку в очередь сессии: listeners в app/database.py после
# commit переносят её в «готовые», после rollback — выбрасывают. Сама
# отправка — dispatch_after_commit(): в app.after_request (см.
# app/__init__.py) и в конце cron-скриптов, пишущих в журнал. Прямо в
# after_commit отправлять нельзя — там сессия не может выполнять SQL, а
# отправке нужны получатели и настройки каналов из БД.

PENDING_KEY = "notifications_pending"
READY_KEY = "notifications_ready"


def after_commit(fn, *args) -> None:
    """Отложить вызов fn(*args) до успешного commit текущей транзакции.
    В args — только id и строки, не ORM-объекты: к моменту отправки они
    уже expired после commit."""
    database.db_session.info.setdefault(PENDING_KEY, []).append((fn, args))


def on_session_commit(session) -> None:
    pending = session.info.pop(PENDING_KEY, None)
    if pending:
        session.info.setdefault(READY_KEY, []).extend(pending)


def on_session_rollback(session) -> None:
    session.info.pop(PENDING_KEY, None)


def dispatch_after_commit() -> None:
    """Выполнить отложенные after_commit() отправки уже закоммиченных
    транзакций. Очередь забирается целиком до отправки: _send для webpush
    сам коммитит (удаление протухшей подписки), а новые записи, попавшие
    в очередь по ходу, дождутся следующего вызова."""
    if database.db_session is None:
        return
    ready = database.db_session.info.pop(READY_KEY, None)
    for fn, args in ready or ():
        try:
            fn(*args)
        except Exception:
            current_app.logger.exception("Не удалось разослать уведомление %s", getattr(fn, "__name__", fn))


def notify_user_id(user_id: int | None, event: str, subject: str, body_text: str) -> None:
    """notify() по id учётной записи — для after_commit()."""
    if user_id is None:
        return
    notify(database.db_session.get(User, user_id), event, subject, body_text)


# ---------------------------------------------------------------------------
# Уведомления правлению о событиях журнала аудита
# ---------------------------------------------------------------------------
#
# Каждый audit.record() ставит событие в очередь (queue_board_event), и
# после commit оно рассылается членам правления, подписанным на группу
# этого действия (User.notify_board_events), кроме самого автора действия.
# Группа ищется по коду действия: сначала точное совпадение, потом префикс
# "раздел.*" — так garage.control_meter_attach попадает в «Электроэнергию»,
# а остальные garage.* — в «Гаражи». Действия из IGNORED_BOARD_ACTIONS не
# рассылаются вовсе. tests/test_board_notifications.py проверяет, что
# каждый код audit.record(...) в коде либо попадает в группу, либо явно
# проигнорирован — новое действие не потеряется молча.

BOARD_EVENT_GROUPS = {
    "approvals": {
        "label": "Ждёт решения правления",
        "actions": ("person.revision_submit", "proposal.create"),
    },
    "bank": {
        "label": "Банк и выписки",
        "actions": ("bank_api.*", "bank_account.*"),
    },
    "finance": {
        "label": "Начисления, платежи, пени",
        "actions": (
            "charge.*", "payment.*", "member_account.*", "penalty.*", "penalty_amnesty.*", "fee_type.*",
            "key_rate.*", "account_format.*", "legal.state_duty_charged", "legal.state_duty_printed",
            "sms.password_reset_charged",
        ),
    },
    "members": {
        "label": "Члены кооператива и учётные записи",
        "actions": ("person.*", "account.*", "role.*", "accountant.*"),
    },
    "garages": {
        "label": "Гаражи и владельцы",
        "actions": ("garage.*", "garage_owner.*", "garage_contact.*", "garage_document.*", "garage_photo.*"),
    },
    "electricity": {
        "label": "Электроэнергия",
        "actions": (
            "electricity_reading.*", "electricity_meter.*", "power.*", "control_meter.*",
            "garage.control_meter_attach", "garage.control_meter_detach",
        ),
    },
    "documents": {
        "label": "Документы, вики, новости, письма",
        "actions": (
            "document.*", "annual_report.*", "reconciliation_act.*", "postal_letter.*", "court_section.*",
            "cooperative.default_court_section", "news.*", "wiki.*", "bulletin.*",
        ),
    },
    "counterparties": {
        "label": "Контрагенты и расходы",
        "actions": ("counterparty.*", "counterparty_payment.*", "expense.*", "counterparty_api.*"),
    },
    "governance": {
        "label": "Собрания, голосования, предложения, состав правления",
        "actions": ("meeting.*", "vote.*", "proposal.*", "board_term.*", "revision_commission.*"),
    },
    "settings": {
        "label": "Почта и настройки",
        "actions": ("cooperative.settings_edit", "mailbox.*", "telegram.*", "sms.*"),
    },
    "logins": {
        "label": "Входы на сайт",
        "actions": ("auth.login",),
    },
    "login_failed": {
        "label": "Неудачные попытки входа",
        "actions": ("auth.login_failed",),
    },
}

# Самое частое и наименее важное — включается только явно.
DEFAULT_BOARD_EVENT_GROUPS = frozenset(BOARD_EVENT_GROUPS) - {"logins"}

# Фоновые синхронизации баланса без изменения данных кооператива и
# действия в чужих разделах, о которых правление и так узнаёт иначе
# (форум — своя подписка notify_forum).
IGNORED_BOARD_ACTIONS = frozenset({
    "bank_api.balance_sync", "counterparty_api.balance_sync",
    "forum.topic_create", "forum.reply_create", "forum.post_edit", "forum.post_delete",
    "forum.topic_delete", "forum.topic_close", "forum.topic_reopen",
    "mailbox.message_trash", "mailbox.message_delete",
})

# Действия, решение по которым принимает только председатель — остальным
# членам правления о них не сообщаем, даже если группа включена.
CHAIRMAN_ONLY_BOARD_ACTIONS = frozenset({"person.revision_submit"})


def board_group_for_action(action: str) -> str | None:
    if action in IGNORED_BOARD_ACTIONS:
        return None
    for code, group in BOARD_EVENT_GROUPS.items():
        if action in group["actions"]:
            return code
    section = action.split(".", 1)[0] + ".*"
    for code, group in BOARD_EVENT_GROUPS.items():
        if section in group["actions"]:
            return code
    return None


def _receives_board_group(user: User, group: str) -> bool:
    return user.is_active and ROLE_LEVEL[user.role] >= ROLE_LEVEL[RoleEnum.BOARD] and group in board_event_groups(user)


def board_event_groups(user: User) -> set[str]:
    """Группы, на которые подписан член правления (с учётом NULL —
    «ещё не выбирал», см. User.notify_board_events)."""
    if user.notify_board_events is None:
        return set(DEFAULT_BOARD_EVENT_GROUPS)
    try:
        codes = json.loads(user.notify_board_events)
    except ValueError:
        return set()
    return {code for code in codes if code in BOARD_EVENT_GROUPS}


def queue_board_event(action: str, summary: str, entity_type: str | None, entity_id: int | None,
                      actor_user_id: int | None, actor_name: str | None) -> None:
    """Вызывается из audit.record() — без commit, см. after_commit()."""
    group = board_group_for_action(action)
    if group is None:
        return
    link = None
    if has_request_context():
        from .audit import entity_url
        path = entity_url(entity_type, entity_id)
        if path:
            link = request.host_url.rstrip("/") + path
    after_commit(_dispatch_board_event, action, group, summary, link, actor_user_id, actor_name)


def _dispatch_board_event(action: str, group: str, summary: str, link: str | None,
                          actor_user_id: int | None, actor_name: str | None) -> None:
    min_level = ROLE_LEVEL[RoleEnum.CHAIRMAN] if action in CHAIRMAN_ONLY_BOARD_ACTIONS else ROLE_LEVEL[RoleEnum.BOARD]
    label = BOARD_EVENT_GROUPS[group]["label"]
    subject = f"{label}: {summary}"
    if len(subject) > 120:
        subject = subject[:119] + "…"
    lines = [summary, ""]
    if actor_name:
        lines.append(f"Кто: {actor_name}")
    if link:
        lines.append(link)
    body_text = "\n".join(lines).rstrip()

    for user in database.db_session.query(User).filter(User.is_active.is_(True)).all():
        if ROLE_LEVEL[user.role] < min_level:
            continue
        if actor_user_id is not None and user.id == actor_user_id:
            continue
        if group not in board_event_groups(user):
            continue
        _deliver(user, f"board:{action}", subject, body_text)
