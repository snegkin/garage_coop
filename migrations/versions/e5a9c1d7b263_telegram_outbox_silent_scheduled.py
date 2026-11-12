"""Очередь Telegram: тихая и отложенная отправка

Revision ID: e5a9c1d7b263
Revises: d2f7b3c8e941
Create Date: 2026-11-12 21:00:00

Пост новости в группу можно отправить без звука (disable_notification) и
не сразу, а в выбранное время (scheduled_at).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a9c1d7b263'
down_revision: Union[str, Sequence[str], None] = 'd2f7b3c8e941'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('telegram_outbox', schema=None) as batch_op:
        batch_op.add_column(sa.Column('silent', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('scheduled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('telegram_outbox', schema=None) as batch_op:
        batch_op.drop_column('scheduled_at')
        batch_op.drop_column('silent')
