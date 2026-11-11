"""Счёт плательщика в строке выписки — для «памяти» плательщика

Revision ID: e1b4c6d9a702
Revises: d5e8a2c71f03
Create Date: 2026-11-11 15:00:00

С Модульбанком нет реестра платежей — плательщика узнаём по выписке.
Счёт плательщика в его банке у человека постоянный: однажды разнесённый
платёж с этого счёта подсказывает лицевой счёт для следующих.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1b4c6d9a702'
down_revision: Union[str, Sequence[str], None] = 'd5e8a2c71f03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bank_statement_line', schema=None) as batch_op:
        batch_op.add_column(sa.Column('counterparty_account', sa.String(length=25), nullable=True))
        batch_op.create_index(batch_op.f('ix_bank_statement_line_counterparty_account'), ['counterparty_account'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('bank_statement_line', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bank_statement_line_counterparty_account'))
        batch_op.drop_column('counterparty_account')
