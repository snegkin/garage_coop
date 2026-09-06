"""
Чат ревизионной комиссии — тот же плавающий виджет, что и чат правления
(см. app/board_chat.py, тот же приём почти дословно), но доступ не по
User.role, а по членству в ТЕКУЩЕЙ (не закрытой) ревизионной комиссии
(RevisionCommission — избирается отдельно от правления, см. docstring в
models.py) — поэтому свой декоратор доступа вместо roles_required.

Опрос новых сообщений (GET /messages) одновременно и есть "прочтение" —
обновляет User.revision_chat_read_at, отдельного роута для этого нет.
"""
import datetime as dt
from functools import wraps

from flask import Blueprint, request, jsonify, g, flash, redirect, url_for

from . import database
from .auth import login_required
from .i18n import translate as _
from .governance import current_revision_commission_member_ids
from .models import RevisionChatMessage, User

bp = Blueprint("revision_chat", __name__, url_prefix="/revision-chat")

MESSAGES_PAGE_SIZE = 50


def revision_commission_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user.person_id is None or g.user.person_id not in current_revision_commission_member_ids():
            flash(_("Недостаточно прав для этого действия."), "danger")
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def _author_name(user: "User") -> str:
    if user.person is not None and user.person.full_name:
        return user.person.full_name
    return user.username


def _serialize(message: RevisionChatMessage) -> dict:
    return {
        "id": message.id,
        "author_name": _author_name(message.author),
        "body": message.body,
        "created_at": message.created_at.isoformat() + "Z",
        "is_mine": message.author_id == g.user.id,
    }


@bp.route("/messages")
@revision_commission_required
def list_messages():
    after_id = request.args.get("after_id", type=int)
    query = database.db_session.query(RevisionChatMessage)
    if after_id is not None:
        messages = query.filter(RevisionChatMessage.id > after_id).order_by(RevisionChatMessage.id).all()
    else:
        messages = query.order_by(RevisionChatMessage.id.desc()).limit(MESSAGES_PAGE_SIZE).all()
        messages.reverse()

    g.user.revision_chat_read_at = dt.datetime.utcnow()
    database.db_session.commit()
    return jsonify(messages=[_serialize(m) for m in messages])


@bp.route("/messages", methods=["POST"])
@revision_commission_required
def send_message():
    body = request.form.get("body", "").strip()
    if not body:
        return jsonify(error="empty"), 400

    message = RevisionChatMessage(author_id=g.user.id, body=body)
    database.db_session.add(message)
    g.user.revision_chat_read_at = dt.datetime.utcnow()
    database.db_session.commit()
    return jsonify(message=_serialize(message))
