"""Telegram: свой адрес Bot API и прокси

Revision ID: c6e1a9d3f582
Revises: b4d8e2f61c37
Create Date: 2026-11-12 18:00:00

api.telegram.org недоступен с VPS в РФ — запросы бота можно пустить через
свой ретранслятор (api_base_url) или прокси (proxy_url_encrypted).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6e1a9d3f582'
down_revision: Union[str, Sequence[str], None] = 'b4d8e2f61c37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('telegram_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('api_base_url', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('proxy_url_encrypted', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('telegram_settings', schema=None) as batch_op:
        batch_op.drop_column('proxy_url_encrypted')
        batch_op.drop_column('api_base_url')
