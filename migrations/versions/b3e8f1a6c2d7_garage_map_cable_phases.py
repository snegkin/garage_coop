"""Схема территории: линии электропитания — фазы у фигуры (garage_map_shape.phases)

Revision ID: b3e8f1a6c2d7
Revises: a9c4e6b2d8f5
Create Date: 2026-11-27 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e8f1a6c2d7'
down_revision: Union[str, Sequence[str], None] = 'a9c4e6b2d8f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('garage_map_shape', schema=None) as batch_op:
        batch_op.add_column(sa.Column('phases', sa.String(length=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('garage_map_shape', schema=None) as batch_op:
        batch_op.drop_column('phases')
