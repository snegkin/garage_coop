"""Архивные расчётные счета

Revision ID: d4a7e2b9c613
Revises: c2f9a7e4d518
Create Date: 2026-11-13 15:00:00

BankAccount.is_archived — закрытый счёт с историей (выписки, реестры,
платежи) не удаляется, а уходит в архив.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4a7e2b9c613'
down_revision: Union[str, Sequence[str], None] = 'c2f9a7e4d518'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bank_account') as batch_op:
        batch_op.add_column(sa.Column('is_archived', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('bank_account') as batch_op:
        batch_op.drop_column('is_archived')
