"""MAX: настройки бота и канал в очереди исходящих

Revision ID: a4c7e2f9b316
Revises: f3b8d2a6c419
Create Date: 2026-11-12 23:00:00

Новости дублируются и в MAX (app/max_bot.py) — через ту же очередь
messenger_outbox, у записи появляется channel; старые записи — telegram.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4c7e2f9b316'
down_revision: Union[str, Sequence[str], None] = 'f3b8d2a6c419'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'max_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bot_token_encrypted', sa.Text(), nullable=True),
        sa.Column('bot_username', sa.String(length=64), nullable=True),
        sa.Column('news_chat_id', sa.String(length=64), nullable=True),
        sa.Column('last_marker', sa.BigInteger(), nullable=True),
        sa.Column('known_chats', sa.Text(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_polled_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_max_settings')),
    )
    with op.batch_alter_table('messenger_outbox', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sa.String(length=16), nullable=False, server_default='telegram'))


def downgrade() -> None:
    with op.batch_alter_table('messenger_outbox', schema=None) as batch_op:
        batch_op.drop_column('channel')
    op.drop_table('max_settings')
