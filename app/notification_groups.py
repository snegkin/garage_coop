"""
Группы уведомлений правления (`/notification-groups/`) — кому уходят
уведомления о действиях на сайте, сбоях cron-синхронизаций (банк, eWeLink,
регистратор) и новой почте правления (см. notifications.BOARD_EVENT_GROUPS).

Это не личная подписка, а состав группы: правление включает в неё группы
пользователей ERP целиком (председатель, правление, бухгалтер,
ревизионная комиссия — состав на момент отправки) и/или отдельных людей с
учётной записью. Хранится в NotificationGroup; пока группу не сохраняли,
действует notifications.DEFAULT_GROUP_ROLES. Каналы доставки (почта,
Telegram, MAX, push) у каждого получателя — его личные, в профиле.
"""
import json

from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import audit, database, notifications
from .auth import roles_required
from .i18n import translate as _
from .models import NotificationGroup, RoleEnum, User

bp = Blueprint("notification_groups", __name__, url_prefix="/notification-groups")


def _user_name(user: User) -> str:
    return user.person.full_name if user.person else user.username


def _ready_channels(user: User) -> list[str]:
    return [
        notifications.CHANNEL_LABELS[channel] for channel in notifications.enabled_channels(user)
        if notifications.channel_is_ready(user, channel)
    ]


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    active_users = database.db_session.query(User).filter(User.is_active.is_(True)).all()
    active_users.sort(key=_user_name)
    groups = []
    for code, meta in notifications.BOARD_EVENT_GROUPS.items():
        group = database.db_session.get(NotificationGroup, code)
        explicit = sorted((u for u in (group.users if group else []) if u.is_active), key=_user_name)
        explicit_ids = {u.id for u in explicit}
        groups.append({
            "code": code,
            "label": meta["label"],
            "roles": notifications.group_roles(group, code),
            "users": explicit,
            "candidates": [u for u in active_users if u.id not in explicit_ids],
            "recipients": [(u, _ready_channels(u)) for u in notifications.group_recipients(code)],
            "chairman_only_note": code == "approvals",
        })
    return render_template("notification_groups/view.html", groups=groups, roles=notifications.RECIPIENT_ROLES)


@bp.route("/<code>", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def save(code):
    if code not in notifications.BOARD_EVENT_GROUPS:
        flash(_("Нет такой группы уведомлений."), "danger")
        return redirect(url_for(".view"))
    f = request.form
    group = database.db_session.get(NotificationGroup, code)
    old_roles = notifications.group_roles(group, code)
    old_users = {u.id: u for u in (group.users if group else [])}
    if group is None:
        group = NotificationGroup(code=code)
        database.db_session.add(group)

    roles = [role for role in notifications.RECIPIENT_ROLES if f.get(f"role_{role}")]
    keep_ids = {int(v) for v in f.getlist("user_id") if v.isdigit()}
    add_id = f.get("add_user_id", "")
    if add_id.isdigit():
        keep_ids.add(int(add_id))
    users = database.db_session.query(User).filter(User.id.in_(keep_ids)).all() if keep_ids else []

    group.roles = json.dumps(roles)
    group.users = users

    changes = []
    added_roles = [notifications.RECIPIENT_ROLES[r] for r in roles if r not in old_roles]
    removed_roles = [notifications.RECIPIENT_ROLES[r] for r in notifications.RECIPIENT_ROLES
                     if r in old_roles and r not in roles]
    new_ids = {u.id for u in users}
    added_users = [_user_name(u) for u in users if u.id not in old_users]
    removed_users = [_user_name(u) for uid, u in old_users.items() if uid not in new_ids]
    if added_roles or added_users:
        changes.append("добавлены: " + ", ".join(added_roles + added_users))
    if removed_roles or removed_users:
        changes.append("убраны: " + ", ".join(removed_roles + removed_users))
    label = notifications.BOARD_EVENT_GROUPS[code]["label"]
    if changes:
        audit.record("notification_group.edit", f"Группа уведомлений «{label}»: " + "; ".join(changes))
    database.db_session.commit()
    flash(_("Состав группы уведомлений сохранён."), "success")
    return redirect(url_for(".view", _anchor=f"group-{code}"))
