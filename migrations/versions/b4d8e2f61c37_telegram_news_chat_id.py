"""Группа Telegram для дублирования новостей

Revision ID: b4d8e2f61c37
Revises: a7c3e9f15b42
Create Date: 2026-11-12 15:00:00

Новость, опубликованная на сайте (app/news.py: create), пересылается ботом
ещё и в группу/канал Telegram (app/telegram_bot.py: publish_news).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4d8e2f61c37'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9f15b42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('telegram_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('news_chat_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('telegram_settings', schema=None) as batch_op:
        batch_op.drop_column('news_chat_id')
