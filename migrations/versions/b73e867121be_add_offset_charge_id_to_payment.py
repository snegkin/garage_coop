"""add offset_charge_id to payment

Revision ID: b73e867121be
Revises: 0f446a1369f4
Create Date: 2026-09-03 00:51:05.635367

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b73e867121be'
down_revision: Union[str, Sequence[str], None] = '0f446a1369f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 'cooperative.garage_area' исключён из auto-generate diff — расхождение
    # локальной dev-БД с уже смёрженной ранее миграцией 2a97e92adbab
    # (remove_garage_area_column), не имеющее отношения к этой правке.
    with op.batch_alter_table('payment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('offset_charge_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_payment_offset_charge_id'), ['offset_charge_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_payment_offset_charge_id_charge'), 'charge', ['offset_charge_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('payment', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_payment_offset_charge_id_charge'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_payment_offset_charge_id'))
        batch_op.drop_column('offset_charge_id')
