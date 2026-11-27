"""Схема территории: доступ к устройству (логин, зашифрованный пароль, порт DVRIP) для настройки камер с сайта

Revision ID: a9c4e6b2d8f5
Revises: f7b1d3e9a2c4
Create Date: 2026-11-27 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9c4e6b2d8f5'
down_revision: Union[str, Sequence[str], None] = 'f7b1d3e9a2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_username', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('device_password_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('device_port', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.drop_column('device_port')
        batch_op.drop_column('device_password_encrypted')
        batch_op.drop_column('device_username')
