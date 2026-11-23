"""payment.gift_expense_id — подарок освобождением от уплаты взносов

Revision ID: d7a3c5e91f02
Revises: c4e8a2f1b937
Create Date: 2026-11-23 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7a3c5e91f02'
down_revision: Union[str, Sequence[str], None] = 'c4e8a2f1b937'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('payment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gift_expense_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_payment_gift_expense_id'), ['gift_expense_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_payment_gift_expense_id_expense'), 'expense',
                                    ['gift_expense_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('payment', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_payment_gift_expense_id_expense'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_payment_gift_expense_id'))
        batch_op.drop_column('gift_expense_id')
