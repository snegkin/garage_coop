"""MAX: привязка аккаунта и уведомления людям

Revision ID: b8e1d4c7a253
Revises: a4c7e2f9b316
Create Date: 2026-11-13 00:00:00

Уведомления о событиях сайта и в MAX (app/max_bot.py): Person.max_user_id —
кому писать (из апдейта bot_started), max_link_token — одноразовый токен
диплинка, User.notify_via_max — галочка в профиле.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e1d4c7a253'
down_revision: Union[str, Sequence[str], None] = 'a4c7e2f9b316'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_user_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('max_link_token', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_person_max_user_id'), ['max_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_person_max_link_token'), ['max_link_token'], unique=True)
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_via_max', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('notify_via_max')
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_person_max_link_token'))
        batch_op.drop_index(batch_op.f('ix_person_max_user_id'))
        batch_op.drop_column('max_link_token')
        batch_op.drop_column('max_user_id')
