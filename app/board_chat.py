"""
Чат правления — общий групповой чат для BOARD/ACCOUNTANT/CHAIRMAN
(is_board()), доступный плавающим виджетом на любой странице (см.
base.html). Простой хронологический лог (BoardChatMessage) — без
вложений. Разметка (ссылки, жирный и т. п.) — app/chat_markup.py: в БД
текст как написан, на сайт и в группы — HTML.

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

Ответ (reply_to) уходит в группы настоящим ответом на копию/оригинал
(id находится при отправке, см. messenger_outbox._send), ответ в группе
приходит на сайт ответом. Правка: своё сообщение с сайта — правятся и
копии (group_edit; копии, ещё ждущие в очереди, просто уходят с новым
текстом); правка в группе (edited_message/message_edited) — меняет текст на
сайте и копию в другой группе. Чужие сообщения в группе бот править не
может, поэтому на сайте правится только сообщение, написанное на сайте.

Реакции (BoardChatReaction): на сайте — эмодзи из REACTIONS (или уже
стоящий на сообщении), повторный клик снимает свою. В группы мессенджеров
с сайта не пересылаются: бот может поставить от себя лишь одну реакцию, без
авторов и количества. Из группы в Telegram — приходят (апдейт
message_reaction, ingest_external_reaction; бот должен быть администратором
группы): от привязанного члена правления — от его имени, иначе — от имени в
Telegram с пометкой.

Опрос отдаёт уже показанные сообщения, изменённые недавно (правка или
реакции, BoardChatMessage.changed_at, то же окно, что и для удалённых), —
виджет перерисовывает их на месте.
"""
import datetime as dt

from flask import Blueprint, request, jsonify, g, abort

from . import audit
from . import chat_markup
from . import database
from .auth import roles_required
from .permissions import is_chairman
from .models import RoleEnum, BoardChatMessage, BoardChatReaction, User, Person, TelegramSettings, MaxSettings

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

# Набор реакций — короткий и фиксированный (как в мессенджерах), порядок —
# порядок кнопок в виджете.
REACTIONS = ("👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "👎")


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


def copy_text(message: BoardChatMessage) -> str:
    """Текст копии сообщения в группе мессенджера — HTML (Telegram
    parse_mode=HTML, MAX "format": "html"), разметка — chat_markup."""
    from markupsafe import escape
    return f"{escape(author_name(message))}: {chat_markup.to_messenger_html(message.body)}"


def relay_to_messengers(message: BoardChatMessage) -> int:
    """Ставит сообщение в очередь всех групп правления, кроме той, откуда
    оно пришло. Без commit. -> число поставленных записей."""
    from . import messenger_outbox
    text = copy_text(message)
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


def _relay_edit(message: BoardChatMessage) -> None:
    """Новый текст — в копии во всех группах, кроме той, откуда сообщение
    пришло (там правит сам автор). Без commit."""
    from . import messenger_outbox
    for source, chat_id in _bridges().items():
        if source != message.source:
            messenger_outbox.enqueue_group_edit(chat_id, message, channel=source, text=copy_text(message))


def _mark_changed(message: BoardChatMessage) -> None:
    message.changed_at = dt.datetime.utcnow()


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


def find_external(source: str, original_id=None, copy_id=None) -> BoardChatMessage | None:
    """Сообщение чата правления по id в группе мессенджера source: либо
    пришедшее оттуда (external_message_id == original_id), либо отправленная
    туда ботом копия (MessengerOutbox.external_id == copy_id). Нужен, чтобы
    ответ в группе привязать к сообщению на сайте."""
    from .models import MessengerOutbox
    from . import messenger_outbox
    if original_id is not None:
        message = database.db_session.query(BoardChatMessage).filter_by(
            source=source, external_message_id=str(original_id)).first()
        if message is not None:
            return message
    if copy_id is not None:
        item = (
            database.db_session.query(MessengerOutbox)
            .filter_by(kind=messenger_outbox.KIND_GROUP, channel=source, external_id=str(copy_id))
            .filter(MessengerOutbox.board_message_id.isnot(None))
            .order_by(MessengerOutbox.id.desc())
            .first()
        )
        if item is not None:
            return item.board_message
    return None


def ingest_external_edit(source: str, external_message_id, text: str) -> BoardChatMessage | None:
    """Правка в группе правления сообщения, пришедшего оттуда, -> новый
    текст на сайте и в копии в другой группе. Неизвестное/удалённое
    сообщение и тот же текст — пропуск. Без commit."""
    body = (text or "").strip()
    if not body or external_message_id is None:
        return None
    message = database.db_session.query(BoardChatMessage).filter_by(
        source=source, external_message_id=str(external_message_id)).first()
    if message is None or message.deleted_at is not None or message.body == body:
        return None
    message.body = body
    message.edited_at = dt.datetime.utcnow()
    _mark_changed(message)
    _relay_edit(message)
    return message


def ingest_external(source: str, external_message_id, external_user_id, display_name: str | None,
                    text: str, reply_to: BoardChatMessage | None = None) -> BoardChatMessage | None:
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
        reply_to=reply_to,
    )
    database.db_session.add(message)
    database.db_session.flush()
    relay_to_messengers(message)
    return message


def _normalize_emoji(emoji: str) -> str:
    """Telegram присылает ❤ без вариационного селектора (U+FE0F), на сайте
    ❤️ с ним — иначе одна и та же реакция легла бы двумя чипами."""
    emoji = (emoji or "").strip()
    if emoji and emoji + "\ufe0f" in REACTIONS:
        return emoji + "\ufe0f"
    return emoji


def ingest_external_reaction(source: str, original_id, copy_id, external_user_id, display_name: str | None,
                             emojis) -> BoardChatMessage | None:
    """Реакции человека из группы правления на сообщение — это его полный
    текущий набор (Telegram присылает new_reaction целиком), поэтому прежние
    реакции из этого мессенджера заменяются им. Реакции, поставленные тем же
    человеком на сайте, не трогаются. Сообщение не из чата (или удалено) —
    пропуск. Без commit. -> сообщение или None."""
    message = find_external(source, original_id=original_id, copy_id=copy_id)
    if message is None or message.deleted_at is not None or external_user_id is None:
        return None
    user = _find_board_user(source, external_user_id)
    wanted = []
    for emoji in emojis:
        emoji = _normalize_emoji(emoji)
        if emoji and len(emoji) <= 16 and emoji not in wanted:
            wanted.append(emoji)
    query = database.db_session.query(BoardChatReaction).filter_by(message_id=message.id, source=source)
    query = query.filter_by(user_id=user.id) if user else query.filter_by(user_id=None, external_user_id=str(external_user_id))
    existing = {row.emoji: row for row in query}
    for emoji, row in existing.items():
        if emoji not in wanted:
            database.db_session.delete(row)
    on_site = set()
    if user:
        on_site = {e for (e,) in database.db_session.query(BoardChatReaction.emoji).filter_by(
            message_id=message.id, user_id=user.id, source=SOURCE_SITE)}
    for emoji in wanted:
        if emoji in existing or emoji in on_site:
            continue
        database.db_session.add(BoardChatReaction(
            message_id=message.id, user_id=user.id if user else None, emoji=emoji, source=source,
            external_user_id=None if user else str(external_user_id),
            external_name=None if user else ((display_name or "").strip()[:128] or None),
        ))
    _mark_changed(message)
    # В одном опросе может прийти и «поставил», и «снял» — следующий апдейт
    # должен видеть уже записанное.
    database.db_session.flush()
    return message


def _reaction_author(row: BoardChatReaction) -> str:
    if row.user is not None:
        return row.user.username
    return f"{row.external_name or '?'} ({SOURCE_LABELS.get(row.source, row.source)})"


def _reactions_by_message(message_ids) -> dict[int, list[dict]]:
    """{message_id: [{emoji, count, mine, users}]} — одним запросом на всю
    пачку сообщений; порядок эмодзи — по первой поставленной реакции."""
    message_ids = list(message_ids)
    if not message_ids:
        return {}
    rows = (
        database.db_session.query(BoardChatReaction)
        .filter(BoardChatReaction.message_id.in_(message_ids))
        .order_by(BoardChatReaction.id)
        .all()
    )
    result: dict[int, dict[str, dict]] = {}
    for row in rows:
        item = result.setdefault(row.message_id, {}).setdefault(
            row.emoji, {"emoji": row.emoji, "count": 0, "mine": False, "users": []})
        item["count"] += 1
        item["users"].append(_reaction_author(row))
        if row.user_id is not None and row.user_id == g.user.id:
            item["mine"] = True
    return {message_id: list(items.values()) for message_id, items in result.items()}


REPLY_EXCERPT = 120


def _serialize_reply(message: BoardChatMessage | None) -> dict | None:
    if message is None:
        return None
    if message.deleted_at is not None:
        return {"id": message.id, "deleted": True}
    plain = chat_markup.to_plain(message.body)
    body = plain if len(plain) <= REPLY_EXCERPT else plain[:REPLY_EXCERPT] + "…"
    return {"id": message.id, "author_name": author_name(message), "body": body}


def _serialize(message: BoardChatMessage, reactions: dict[int, list[dict]] | None = None) -> dict:
    if reactions is None:
        reactions = _reactions_by_message([message.id]) if message.id else {}
    return {
        "id": message.id,
        "author_name": author_name(message),
        "source": message.source,
        "body": message.body,  # как написан — для правки
        "body_html": chat_markup.to_html(message.body),
        "body_plain": chat_markup.to_plain(message.body),  # закреп, плашка ответа
        # "Z" — created_at в БД наивный UTC (см. models.py), суффикс
        # делает JS-Date() на клиенте однозначным (тот же приём, что и в
        # electricity_monitor.py: history-data).
        "created_at": message.created_at.isoformat() + "Z",
        "is_mine": message.author_id == g.user.id,
        "can_delete": _can_delete(message),
        "can_edit": _can_edit(message),
        "edited": message.edited_at is not None,
        "pinned": message.pinned_at is not None,
        "reply_to": _serialize_reply(message.reply_to),
        "reactions": reactions.get(message.id, []),
    }


def _can_delete(message: BoardChatMessage) -> bool:
    return (message.author_id is not None and message.author_id == g.user.id) or is_chairman()


def _can_edit(message: BoardChatMessage) -> bool:
    """Только своё и только написанное на сайте: копию сообщения из группы
    бот поправить может, а оригинал автора в группе — нет."""
    return message.source == SOURCE_SITE and message.author_id is not None and message.author_id == g.user.id


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
    себя), недавно изменённые (правка/реакции — виджет перерисовывает их,
    если показывает) и текущее закреплённое (null — нет)."""
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
    # Изменённые недавно из тех, что виджет уже показывает (id <= after_id;
    # новые и так придут целиком).
    changed = query.filter(
        BoardChatMessage.id <= after_id,
        BoardChatMessage.changed_at > now - DELETED_WINDOW,
    ).order_by(BoardChatMessage.id).all() if after_id is not None else []
    reactions = _reactions_by_message({m.id for m in messages} | {m.id for m in changed})

    g.user.board_chat_read_at = now
    database.db_session.commit()
    return jsonify(
        messages=[_serialize(m, reactions) for m in messages],
        deleted_ids=deleted_ids,
        pinned=_serialize(pinned, reactions) if pinned else None,
        changed=[_serialize(m, reactions) for m in changed],
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


@bp.route("/messages/<int:message_id>/react", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def react_message(message_id):
    """Ставит реакцию или снимает свою такую же (переключатель). Кроме
    набора REACTIONS — эмодзи, уже стоящий на сообщении (пришёл из Telegram):
    к нему можно присоединиться, нажав на чип."""
    message = _get_message(message_id)
    emoji = request.form.get("emoji", "")
    if emoji not in REACTIONS and not database.db_session.query(BoardChatReaction.id).filter_by(
            message_id=message.id, emoji=emoji).first():
        return jsonify(error="emoji"), 400
    existing = database.db_session.query(BoardChatReaction).filter_by(
        message_id=message.id, user_id=g.user.id, emoji=emoji).first()
    if existing is not None:
        database.db_session.delete(existing)
    else:
        database.db_session.add(BoardChatReaction(message_id=message.id, user_id=g.user.id, emoji=emoji))
    _mark_changed(message)
    database.db_session.commit()
    return jsonify(ok=True, reactions=_reactions_by_message([message.id]).get(message.id, []))


@bp.route("/messages/<int:message_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_message(message_id):
    message = _get_message(message_id)
    if not _can_edit(message):
        abort(403)
    body = request.form.get("body", "").strip()
    if not body:
        return jsonify(error="empty"), 400
    if body != message.body:
        message.body = body
        message.edited_at = dt.datetime.utcnow()
        _mark_changed(message)
        _relay_edit(message)
        database.db_session.commit()
    return jsonify(message=_serialize(message))


@bp.route("/messages", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def send_message():
    body = request.form.get("body", "").strip()
    if not body:
        return jsonify(error="empty"), 400
    reply_to = None
    reply_to_id = request.form.get("reply_to_id", type=int)
    if reply_to_id:
        reply_to = database.db_session.get(BoardChatMessage, reply_to_id)
        if reply_to is None or reply_to.deleted_at is not None:
            reply_to = None  # успели удалить — отправляем обычным сообщением

    message = BoardChatMessage(author_id=g.user.id, author=g.user, body=body, source=SOURCE_SITE,
                               reply_to=reply_to)
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
            # Когда последний раз был в чате (читал/писал — board_chat_read_at
            # обновляет опрос открытой панели и отправка), как «был(а)…» в
            # Telegram; виджет показывает у тех, у кого чат сейчас не открыт.
            "in_chat_at": user.board_chat_read_at.isoformat() + "Z" if user.board_chat_read_at else None,
        })
    return jsonify(participants=result)
