"""Группы уведомлений правления (notification_group) вместо личной подписки user.notify_board_events

Кому уходят уведомления правления, теперь задаёт состав группы: группы
пользователей ERP целиком (roles) и отдельные люди (notification_group_user),
— а не каждый член правления у себя в профиле. Нынешние подписки
переносятся как есть, с одним изменением: «Сбои синхронизации» больше не
включены по умолчанию — кто их явно не выбирал, перестаёт получать.

Группа, которую получают все активные председатель/правление/бухгалтер,
переносится как «эти группы пользователей целиком» (для групп по умолчанию
— вовсе без строки, действует notifications.DEFAULT_GROUP_ROLES); иначе —
поимённым списком.

Revision ID: e5b9c1d4a7f3
Revises: d7a3c5e91f02
Create Date: 2026-11-23 18:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5b9c1d4a7f3'
down_revision: Union[str, Sequence[str], None] = 'd7a3c5e91f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Коды notifications.BOARD_EVENT_GROUPS на момент миграции.
GROUP_CODES = (
    "approvals", "bank", "finance", "members", "garages", "electricity", "documents", "counterparties",
    "governance", "settings", "logins", "login_failed", "mail", "failures",
)
OLD_DEFAULT = set(GROUP_CODES) - {"logins"}
NEW_DEFAULT = set(GROUP_CODES) - {"logins", "failures"}
BOARD_ROLES = ("CHAIRMAN", "BOARD", "ACCOUNTANT")  # имена RoleEnum, как их хранит sa.Enum
ROLE_CODES = ["chairman", "board", "accountant"]


def upgrade() -> None:
    op.create_table(
        'notification_group',
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('roles', sa.Text(), nullable=False, server_default='[]'),
        sa.PrimaryKeyConstraint('code', name=op.f('pk_notification_group')),
    )
    op.create_table(
        'notification_group_user',
        sa.Column('group_code', sa.String(length=50), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['group_code'], ['notification_group.code'],
                                name=op.f('fk_notification_group_user_group_code_notification_group'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'],
                                name=op.f('fk_notification_group_user_user_id_user'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('group_code', 'user_id', name=op.f('pk_notification_group_user')),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        'SELECT id, notify_board_events FROM "user" WHERE is_active = :active AND role IN :roles'
    ).bindparams(sa.bindparam("roles", expanding=True)), {"active": True, "roles": list(BOARD_ROLES)}).fetchall()
    subscriptions = {}
    for user_id, raw in rows:
        if raw is None:
            codes = OLD_DEFAULT & NEW_DEFAULT
        else:
            try:
                codes = set(json.loads(raw))
            except ValueError:
                codes = set()
        subscriptions[user_id] = codes
    everyone = set(subscriptions)

    for code in GROUP_CODES:
        receivers = {uid for uid, codes in subscriptions.items() if code in codes}
        if receivers == everyone:
            if code in NEW_DEFAULT or not receivers:
                continue
            bind.execute(sa.text("INSERT INTO notification_group (code, roles) VALUES (:code, :roles)"),
                         {"code": code, "roles": json.dumps(ROLE_CODES)})
            continue
        bind.execute(sa.text("INSERT INTO notification_group (code, roles) VALUES (:code, '[]')"), {"code": code})
        for uid in sorted(receivers):
            bind.execute(sa.text("INSERT INTO notification_group_user (group_code, user_id) VALUES (:code, :uid)"),
                         {"code": code, "uid": uid})

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('notify_board_events')


def downgrade() -> None:
    """Обратно — личные подписки председателя/правления/бухгалтера по
    составу групп (ревизионная комиссия и люди без этих ролей теряются)."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_board_events', sa.Text(), nullable=True))

    bind = op.get_bind()
    groups = {code: json.loads(roles or "[]") for code, roles in
              bind.execute(sa.text("SELECT code, roles FROM notification_group")).fetchall()}
    explicit = {}
    for code, uid in bind.execute(sa.text("SELECT group_code, user_id FROM notification_group_user")).fetchall():
        explicit.setdefault(code, set()).add(uid)
    users = bind.execute(sa.text('SELECT id, role FROM "user" WHERE role IN :roles').bindparams(
        sa.bindparam("roles", expanding=True)), {"roles": list(BOARD_ROLES)}).fetchall()
    for uid, role in users:
        codes = []
        for code in GROUP_CODES:
            roles = groups[code] if code in groups else (ROLE_CODES if code in NEW_DEFAULT else [])
            if role.lower() in roles or uid in explicit.get(code, set()):
                codes.append(code)
        bind.execute(sa.text('UPDATE "user" SET notify_board_events = :v WHERE id = :id'),
                     {"v": json.dumps(codes), "id": uid})

    op.drop_table('notification_group_user')
    op.drop_table('notification_group')
