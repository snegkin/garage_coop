"""Очередь исходящих сообщений Telegram

Revision ID: d2f7b3c8e941
Revises: c6e1a9d3f582
Create Date: 2026-11-12 20:00:00

Сообщения бота (новости в группу, уведомления) уходят через очередь с
повторами (app/telegram_outbox.py, cron scripts/poll_telegram.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2f7b3c8e941'
down_revision: Union[str, Sequence[str], None] = 'c6e1a9d3f582'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'telegram_outbox',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('chat_id', sa.String(length=64), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('news_id', sa.Integer(), nullable=True),
        sa.Column('url', sa.String(length=512), nullable=True),
        sa.Column('album_first', sa.Boolean(), nullable=False),
        sa.Column('steps_done', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'sent', 'uncertain', 'failed', 'cancelled',
                                    name='telegramoutboxstatus', native_enum=False, length=16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['news_id'], ['news.id'], name=op.f('fk_telegram_outbox_news_id_news'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_telegram_outbox')),
    )
    with op.batch_alter_table('telegram_outbox', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_telegram_outbox_news_id'), ['news_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_telegram_outbox_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_telegram_outbox_next_attempt_at'), ['next_attempt_at'], unique=False)


def downgrade() -> None:
    op.drop_table('telegram_outbox')
