"""
Чат правления — общий групповой чат для BOARD/ACCOUNTANT/CHAIRMAN
(is_board()), доступный плавающим виджетом на любой странице (см.
base.html). Простой хронологический лог (BoardChatMessage) — без
редактирования/удаления сообщений, без вложений, без markdown-разметки.

Опрос новых сообщений (GET /messages) одновременно и есть "прочтение" —
обновляет User.board_chat_read_at, отдельного роута для этого нет (см.
докстринг поля в models.py).
"""
import datetime as dt

from flask import Blueprint, request, jsonify, g

from . import database
from .auth import roles_required
from .models import RoleEnum, BoardChatMessage, User

bp = Blueprint("board_chat", __name__, url_prefix="/board-chat")

MESSAGES_PAGE_SIZE = 50


def _author_name(user: "User") -> str:
    if user.person is not None and user.person.full_name:
        return user.person.full_name
    return user.username


def _serialize(message: BoardChatMessage) -> dict:
    return {
        "id": message.id,
        "author_name": _author_name(message.author),
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
