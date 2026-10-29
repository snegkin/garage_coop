"""counterparty prepaid and expense kind

Расчёты по предоплате (SMS Aero, Почта России): Counterparty.prepaid и
Expense.kind — счёт на оплату (INVOICE) не начисление и на баланс
расчётов не влияет, начисление (CHARGE) — только по акту/УПД. Все
существующие расходы остаются начислениями, все контрагенты — на
постоплате, т.е. ничего не меняется, пока галочку не поставят вручную.

Revision ID: e5b81c3d9a42
Revises: c4a9e1f37b20
Create Date: 2026-10-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b81c3d9a42'
down_revision: Union[str, Sequence[str], None] = 'c4a9e1f37b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.add_column(sa.Column('prepaid', sa.Boolean(), server_default='0', nullable=False))
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'kind', sa.Enum('CHARGE', 'INVOICE', name='expensekind'),
            server_default='CHARGE', nullable=False,
        ))


def downgrade() -> None:
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.drop_column('kind')
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.drop_column('prepaid')
