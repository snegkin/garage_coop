"""Время последнего веб-хука банка

Revision ID: a7c3e9f15b42
Revises: e1b4c6d9a702
Create Date: 2026-11-12 12:00:00

Уведомления Модульбанка о новых операциях (app/bank_webhooks.py): в окне
настроек API показываем, когда пришло последнее, — так видно, что адрес в
ЛК банка вписан правильно.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c3e9f15b42'
down_revision: Union[str, Sequence[str], None] = 'e1b4c6d9a702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bank_api_credential', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_webhook_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('bank_api_credential', schema=None) as batch_op:
        batch_op.drop_column('last_webhook_at')
