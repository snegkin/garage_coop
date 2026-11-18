"""Почта правления: сброс известных писем после переезда на gm-voshod.ru

Revision ID: c4e8b2a6d917
Revises: a7d2c9e4f150
Create Date: 2026-11-18 13:00:00

Письма перенесены imapsync с pravlenie@гм-восход.рф на
pravlenie@gm-voshod.ru, логин в настройках уже сменён. На новом ящике у
писем другие UID, поэтому MailboxSettings.known_message_ids от старого
ящика сравнивать не с чем: cron-опрос принял бы перенесённые письма за
новые и разослал правлению уведомления. NULL — «опроса ещё не было»,
ближайший опрос запомнит ящик как есть, без уведомлений. Дальше то же
делает сохранение настроек при смене сервера или логина
(app/mailbox.py: save_settings).

Откат не нужен: старый список к новому ящику не подходит.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e8b2a6d917'
down_revision: Union[str, Sequence[str], None] = 'a7d2c9e4f150'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE mailbox_settings SET known_message_ids = NULL, unread_count = 0, last_error = NULL"))


def downgrade() -> None:
    pass
