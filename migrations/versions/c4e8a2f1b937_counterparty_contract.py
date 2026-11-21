"""Договор с контрагентом: counterparty.contract_number, contract_date

Revision ID: c4e8a2f1b937
Revises: f8bf97f87b5b
Create Date: 2026-11-21 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e8a2f1b937'
down_revision: Union[str, Sequence[str], None] = 'f8bf97f87b5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.add_column(sa.Column('contract_number', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('contract_date', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.drop_column('contract_date')
        batch_op.drop_column('contract_number')
