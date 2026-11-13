"""
Чат правления — общий групповой чат для BOARD/ACCOUNTANT/CHAIRMAN
(is_board()), доступный плавающим виджетом на любой странице (см.
base.html). Простой хронологический лог (BoardChatMessage) — без
редактирования/удаления сообщений, без вложений, без markdown-разметки.

Опрос новых сообщений (GET /messages) одновременно и есть "прочтение" —
обновляет User.board_chat_read_at, отдельного роута для этого нет (см.
докстринг поля в models.py).

Список участников (GET /participants) — кто сейчас онлайн и у кого открыта
панель чата. Присутствие обновляется heartbeat'ом (POST /heartbeat),
который виджет шлёт с любой страницы сайта, а не только пока сам чат
открыт (см. base.html: initChatWidget) — иначе человек, не открывавший
чат, всегда выглядел бы офлайн, даже активно работая на других страницах.

Мост с закрытыми группами правления в Telegram и MAX
(TelegramSettings/MaxSettings.board_chat_id): сообщение с сайта ставится в
очередь мессенджеров (messenger_outbox.enqueue_group_message), сообщение из
группы приходит при опросе ботов (scripts/poll_messengers.py, раз в минуту)
через ingest_external и пересылается в остальные точки. Автор из
мессенджера — член правления с этим привязанным Telegram/MAX (Person.
telegram_chat_id/max_user_id), иначе имя из мессенджера с пометкой.
Петли нет: сообщение никогда не отправляется туда, откуда пришло, а
собственные сообщения бота при опросе пропускаются.

Удаление (своё — автор, любое — председатель) и закреп (одно сообщение,
любой член правления) повторяются в группах — очередь
(messenger_outbox.enqueue_group_action), по id копии, запомненному при
отправке. Из мессенджеров на сайт удаление и закреп не переносятся: Bot API
Telegram об удалениях ботам не сообщает.
"""
import datetime as dt

from flask import Blueprint, request, jsonify, g, abort

from . import audit
from . import database
from .auth import roles_required
from .permissions import is_chairman
from .models import RoleEnum, BoardChatMessage, User, Person, TelegramSettings, MaxSettings

bp = Blueprint("board_chat", __name__, url_prefix="/board-chat")

MESSAGES_PAGE_SIZE = 50

# Heartbeat шлётся раз в BOARD_CHAT_HEARTBEAT_INTERVAL_MS (см. base.html) —
# порог "онлайн" чуть больше интервала, чтобы один пропущенный по сети
# heartbeat не сразу показывал человека офлайн.
ONLINE_THRESHOLD = dt.timedelta(seconds=90)


BOARD_ROLES = (RoleEnum.BOARD, RoleEnum.ACCOUNTANT, RoleEnum.CHAIRMAN)

SOURCE_SITE = "site"
SOURCE_TELEGRAM = "telegram"
SOURCE_MAX = "max"
SOURCE_LABELS = {SOURCE_TELEGRAM: "TG", SOURCE_MAX: "MAX"}


def author_name(message: BoardChatMessage) -> str:
    # Логин, не ФИО — в чате правления участников и так немного, полное
    # имя только удлиняет строку; кто есть кто, видно и по логину.
    if message.author is not None:
        return message.author.username
    return f"{message.external_author or '?'} ({SOURCE_LABELS.get(message.source, message.source)})"


def _bridges() -> dict[str, str]:
    """{source: board_chat_id} мессенджеров, где мост включён (бот настроен
    и группа правления задана)."""
    from . import max_bot, telegram_bot
    result = {}
    tg = database.db_session.query(TelegramSettings).first()
    if telegram_bot.is_configured(tg) and tg.board_chat_id:
        result[SOURCE_TELEGRAM] = tg.board_chat_id
    mx = database.db_session.query(MaxSettings).first()
    if max_bot.is_configured(mx) and mx.board_chat_id:
        result[SOURCE_MAX] = mx.board_chat_id
    return result


def relay_to_messengers(message: BoardChatMessage) -> int:
    """Ставит сообщение в очередь всех групп правления, кроме той, откуда
    оно пришло. Без commit. -> число поставленных записей."""
    from . import messenger_outbox
    text = f"{author_name(message)}: {message.body}"
    count = 0
    for source, chat_id in _bridges().items():
        if source == message.source:
            continue
        messenger_outbox.enqueue_group_message(chat_id, text, channel=source, board_message=message)
        count += 1
    return count


def _relay_action(kind: str, message: BoardChatMessage) -> None:
    """Удаление/закреп/открепление — во всех группах, включая ту, откуда
    сообщение пришло (там удаляется/закрепляется оригинал). Без commit."""
    from . import messenger_outbox
    for source, chat_id in _bridges().items():
        messenger_outbox.enqueue_group_action(kind, chat_id, message, channel=source)


def is_board_chat(source: str, chat_id) -> bool:
    """Этот чат мессенджера — группа правления (мост включён)?"""
    if chat_id is None:
        return False
    return _bridges().get(source) == str(chat_id)


def _find_board_user(source: str, external_user_id) -> User | None:
    if external_user_id is None:
        return None
    column = Person.telegram_chat_id if source == SOURCE_TELEGRAM else Person.max_user_id
    return (
        database.db_session.query(User)
        .join(Person, User.person_id == Person.id)
        .filter(column == int(external_user_id), User.role.in_(BOARD_ROLES), User.is_active.is_(True))
        .first()
    )


def ingest_external(source: str, external_message_id, external_user_id, display_name: str | None,
                    text: str) -> BoardChatMessage | None:
    """Сообщение из группы правления в мессенджере -> чат правления на сайте
    и пересылка в другие группы. Повторный апдейт (тот же id) и пустой
    текст пропускаются. Без commit — вызывающий (опрос бота) коммитит вместе
    с курсором апдейтов. -> созданное сообщение или None."""
    body = (text or "").strip()
    if not body:
        return None
    external_message_id = str(external_message_id) if external_message_id is not None else None
    if external_message_id is not None:
        exists = database.db_session.query(BoardChatMessage.id).filter_by(
            source=source, external_message_id=external_message_id).first()
        if exists:
            return None
    user = _find_board_user(source, external_user_id)
    message = BoardChatMessage(
        author_id=user.id if user else None,
        author=user,
        body=body,
        source=source,
        external_author=None if user else ((display_name or "").strip()[:128] or None),
        external_message_id=external_message_id,
    )
    database.db_session.add(message)
    database.db_session.flush()
    relay_to_messengers(message)
    return message


def _serialize(message: BoardChatMessage) -> dict:
    return {
        "id": message.id,
        "author_name": author_name(message),
        "source": message.source,
        "body": message.body,
        # "Z" — created_at в БД наивный UTC (см. models.py), суффикс
        # делает JS-Date() на клиенте однозначным (тот же приём, что и в
        # electricity_monitor.py: history-data).
        "created_at": message.created_at.isoformat() + "Z",
        "is_mine": message.author_id == g.user.id,
        "can_delete": _can_delete(message),
        "pinned": message.pinned_at is not None,
    }


def _can_delete(message: BoardChatMessage) -> bool:
    return (message.author_id is not None and message.author_id == g.user.id) or is_chairman()


def _pinned() -> BoardChatMessage | None:
    return (
        database.db_session.query(BoardChatMessage)
        .filter(BoardChatMessage.pinned_at.isnot(None), BoardChatMessage.deleted_at.is_(None))
        .order_by(BoardChatMessage.pinned_at.desc())
        .first()
    )


# Опрос раз в несколько секунд, пока панель открыта; при открытии панель
# перечитывает всё заново — удалённое раньше этого окна она и так не покажет.
DELETED_WINDOW = dt.timedelta(minutes=10)


@bp.route("/messages")
@roles_required(RoleEnum.BOARD)
def list_messages():
    """Кроме новых сообщений — id недавно удалённых (виджет убирает их у
    себя) и текущее закреплённое (null — нет)."""
    after_id = request.args.get("after_id", type=int)
    query = database.db_session.query(BoardChatMessage).filter(BoardChatMessage.deleted_at.is_(None))
    if after_id is not None:
        messages = query.filter(BoardChatMessage.id > after_id).order_by(BoardChatMessage.id).all()
    else:
        messages = query.order_by(BoardChatMessage.id.desc()).limit(MESSAGES_PAGE_SIZE).all()
        messages.reverse()
    now = dt.datetime.utcnow()
    deleted_ids = [
        row.id for row in database.db_session.query(BoardChatMessage.id)
        .filter(BoardChatMessage.deleted_at > now - DELETED_WINDOW)
    ]
    pinned = _pinned()

    g.user.board_chat_read_at = now
    database.db_session.commit()
    return jsonify(
        messages=[_serialize(m) for m in messages],
        deleted_ids=deleted_ids,
        pinned=_serialize(pinned) if pinned else None,
    )


def _get_message(message_id: int) -> BoardChatMessage:
    message = database.db_session.get(BoardChatMessage, message_id)
    if message is None or message.deleted_at is not None:
        abort(404)
    return message


@bp.route("/messages/<int:message_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_message(message_id):
    message = _get_message(message_id)
    if not _can_delete(message):
        abort(403)
    message.deleted_at = dt.datetime.utcnow()
    message.deleted_by_id = g.user.id
    message.pinned_at = None
    # Копии, ещё ждущие отправки, просто не отправляем — удалять будет нечего.
    from . import messenger_outbox
    from .models import MessengerOutbox, MessengerOutboxStatus
    database.db_session.query(MessengerOutbox).filter_by(
        kind=messenger_outbox.KIND_GROUP, board_message_id=message.id, status=MessengerOutboxStatus.PENDING,
    ).update({"status": MessengerOutboxStatus.CANCELLED})
    _relay_action("group_delete", message)
    audit.record(
        "board_chat.delete",
        f"Удалено сообщение чата правления #{message.id} (автор: {author_name(message)})",
        entity_type="board_chat_message", entity_id=message.id,
    )
    database.db_session.commit()
    return jsonify(ok=True)


@bp.route("/messages/<int:message_id>/pin", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def pin_message(message_id):
    """Закреплено одно сообщение: прежнее открепляется (и в группах тоже —
    в Telegram закрепов может быть несколько, держим как на сайте)."""
    message = _get_message(message_id)
    previous = _pinned()
    if previous is not None and previous.id != message.id:
        previous.pinned_at = None
        _relay_action("group_unpin", previous)
    if previous is None or previous.id != message.id:
        message.pinned_at = dt.datetime.utcnow()
        message.pinned_by_id = g.user.id
        _relay_action("group_pin", message)
    database.db_session.commit()
    return jsonify(ok=True, pinned=_serialize(message))


@bp.route("/messages/<int:message_id>/unpin", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def unpin_message(message_id):
    message = _get_message(message_id)
    if message.pinned_at is not None:
        message.pinned_at = None
        _relay_action("group_unpin", message)
        database.db_session.commit()
    return jsonify(ok=True)


@bp.route("/messages", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def send_message():
    body = request.form.get("body", "").strip()
    if not body:
        return jsonify(error="empty"), 400

    message = BoardChatMessage(author_id=g.user.id, author=g.user, body=body, source=SOURCE_SITE)
    database.db_session.add(message)
    relay_to_messengers(message)
    g.user.board_chat_read_at = dt.datetime.utcnow()
    database.db_session.commit()
    return jsonify(message=_serialize(message))


@bp.route("/heartbeat", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def heartbeat():
    """Отмечает, что человек сейчас на сайте (для списка участников) и
    открыта ли у него именно панель чата — шлётся с любой страницы, пока
    виджет загружен, независимо от того, открыта ли панель (см. docstring
    модуля)."""
    g.user.board_chat_last_seen_at = dt.datetime.utcnow()
    g.user.board_chat_open = request.form.get("open") == "1"
    database.db_session.commit()
    return jsonify(ok=True)


@bp.route("/participants")
@roles_required(RoleEnum.BOARD)
def participants():
    users = (
        database.db_session.query(User)
        .filter(User.role.in_(BOARD_ROLES), User.is_active.is_(True))
        .order_by(User.username)
        .all()
    )
    now = dt.datetime.utcnow()
    result = []
    for user in users:
        online = user.board_chat_last_seen_at is not None and now - user.board_chat_last_seen_at <= ONLINE_THRESHOLD
        result.append({
            "username": user.username,
            "is_mine": user.id == g.user.id,
            "online": online,
            # Панель считается открытой, только пока человек ещё и онлайн —
            # иначе тот, кто закрыл вкладку не свернув чат явно, навсегда
            # висел бы «открыт» (см. докстринг board_chat_open в models.py).
            "chat_open": online and user.board_chat_open,
        })
    return jsonify(participants=result)
