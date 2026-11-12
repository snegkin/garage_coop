"""Очередь исходящих: telegram_outbox -> messenger_outbox

Revision ID: f3b8d2a6c419
Revises: e5a9c1d7b263
Create Date: 2026-11-12 22:00:00

Через ту же очередь (app/messenger_outbox.py) пойдут и другие мессенджеры
(MAX), поэтому таблица получает нейтральное имя. Данные не меняются.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f3b8d2a6c419'
down_revision: Union[str, Sequence[str], None] = 'e5a9c1d7b263'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXED = ('news_id', 'status', 'next_attempt_at')


def _rename(old: str, new: str) -> None:
    for column in _INDEXED:
        op.drop_index(f'ix_{old}_{column}', table_name=old)
    op.rename_table(old, new)
    for column in _INDEXED:
        op.create_index(f'ix_{new}_{column}', new, [column], unique=False)


def upgrade() -> None:
    _rename('telegram_outbox', 'messenger_outbox')


def downgrade() -> None:
    _rename('messenger_outbox', 'telegram_outbox')
