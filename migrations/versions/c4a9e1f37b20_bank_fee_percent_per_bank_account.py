"""bank fee percent per bank account

Комиссия банка за зачисление (bank_fee_percent) переезжает с кооператива
на расчётный счёт: у каждого банка свой процент (Сбербанк 1,6%, ВТБ
«Простой процент» 1%), и надбавка в сумме к оплате должна зависеть от
того, на какой счёт придёт платёж. Значение кооператива копируется во все
существующие счета — до этой миграции оно и так действовало для них всех.

Revision ID: c4a9e1f37b20
Revises: b7e2d4f1a903
Create Date: 2026-10-27 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a9e1f37b20'
down_revision: Union[str, Sequence[str], None] = 'b7e2d4f1a903'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bank_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bank_fee_percent', sa.Numeric(precision=5, scale=3), nullable=True))
    op.execute(
        "UPDATE bank_account SET bank_fee_percent = "
        "(SELECT bank_fee_percent FROM cooperative ORDER BY id LIMIT 1)"
    )
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.drop_column('bank_fee_percent')


def downgrade() -> None:
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bank_fee_percent', sa.Numeric(precision=5, scale=3), nullable=True))
    # Обратно — процент основного счёта (на него выставляются ПД-4).
    op.execute(
        "UPDATE cooperative SET bank_fee_percent = "
        "(SELECT bank_fee_percent FROM bank_account ORDER BY is_primary DESC, id LIMIT 1)"
    )
    with op.batch_alter_table('bank_account', schema=None) as batch_op:
        batch_op.drop_column('bank_fee_percent')
