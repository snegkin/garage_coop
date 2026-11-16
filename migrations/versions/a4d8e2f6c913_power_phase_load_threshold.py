"""Мониторинг фаз: порог фонового потребления (индикатор нагрузки)

Revision ID: a4d8e2f6c913
Revises: e9c4f2b6d531
Create Date: 2026-11-16 23:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4d8e2f6c913'
down_revision: Union[str, Sequence[str], None] = 'e9c4f2b6d531'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('power_phase_device') as batch_op:
        batch_op.add_column(sa.Column('load_threshold_w', sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column('load_threshold_offset_w', sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column('lights_w', sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('power_phase_device') as batch_op:
        batch_op.drop_column('lights_w')
        batch_op.drop_column('load_threshold_offset_w')
        batch_op.drop_column('load_threshold_w')
