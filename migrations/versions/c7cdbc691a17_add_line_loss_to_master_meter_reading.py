"""add line_loss to master meter reading

Потери в линии (кВт·ч) из счёта энергосбыта — по прямой просьбе: сумма к
оплате должна считаться не по чистой дельте показаний общего счётчика, а
по «начисленному объёму» = дельта + потери, как в самом счёте энергосбыта
(проверено на реальном примере: 38267 - 37696 = 571, + 14.529 потерь =
585.529, что точно совпадает с «начисленным объёмом» в счёте ТНС Энерго).

Revision ID: c7cdbc691a17
Revises: d8cd9b57ae71
Create Date: 2026-09-16 17:29:19.299584

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7cdbc691a17'
down_revision: Union[str, Sequence[str], None] = 'd8cd9b57ae71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('master_meter_reading', schema=None) as batch_op:
        batch_op.add_column(sa.Column('line_loss', sa.Numeric(precision=14, scale=2), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('master_meter_reading', schema=None) as batch_op:
        batch_op.drop_column('line_loss')
