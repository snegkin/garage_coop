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
"""
import datetime as dt

from flask import Blueprint, request, jsonify, g

from . import database
from .auth import roles_required
from .models import RoleEnum, BoardChatMessage, User

bp = Blueprint("board_chat", __name__, url_prefix="/board-chat")

MESSAGES_PAGE_SIZE = 50

# Heartbeat шлётся раз в BOARD_CHAT_HEARTBEAT_INTERVAL_MS (см. base.html) —
# порог "онлайн" чуть больше интервала, чтобы один пропущенный по сети
# heartbeat не сразу показывал человека офлайн.
ONLINE_THRESHOLD = dt.timedelta(seconds=90)


def _serialize(message: BoardChatMessage) -> dict:
    return {
        "id": message.id,
        # Логин, не ФИО — в чате правления участников и так немного, полное
        # имя только удлиняет строку; кто есть кто, видно и по логину.
        "author_name": message.author.username,
        "body": message.body,
        # "Z" — created_at в БД наивный UTC (см. models.py), суффикс
        # делает JS-Date() на клиенте однозначным (тот же приём, что и в
        # electricity_monitor.py: history-data).
        "created_at": message.created_at.isoformat() + "Z",
        "is_mine": message.author_id == g.user.id,
    }


@bp.route("/messages")
@roles_required(RoleEnum.BOARD)
def list_messages():
    after_id = request.args.get("after_id", type=int)
    query = database.db_session.query(BoardChatMessage)
    if after_id is not None:
        messages = query.filter(BoardChatMessage.id > after_id).order_by(BoardChatMessage.id).all()
    else:
        messages = query.order_by(BoardChatMessage.id.desc()).limit(MESSAGES_PAGE_SIZE).all()
        messages.reverse()

    g.user.board_chat_read_at = dt.datetime.utcnow()
    database.db_session.commit()
    return jsonify(messages=[_serialize(m) for m in messages])


@bp.route("/messages", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def send_message():
    body = request.form.get("body", "").strip()
    if not body:
        return jsonify(error="empty"), 400

    message = BoardChatMessage(author_id=g.user.id, body=body)
    database.db_session.add(message)
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
        .filter(User.role.in_([RoleEnum.BOARD, RoleEnum.ACCOUNTANT, RoleEnum.CHAIRMAN]), User.is_active.is_(True))
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
