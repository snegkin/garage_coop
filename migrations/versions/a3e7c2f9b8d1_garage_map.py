"""Схема территории: контуры боксов, проездов и зданий (garage_map_shape) и параметры полотна

Revision ID: a3e7c2f9b8d1
Revises: e5b9c1d4a7f3
Create Date: 2026-11-25 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3e7c2f9b8d1'
down_revision: Union[str, Sequence[str], None] = 'e5b9c1d4a7f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'garage_map_shape',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='garage'),
        sa.Column('garage_id', sa.Integer(), nullable=True),
        sa.Column('label', sa.String(length=100), nullable=True),
        sa.Column('points', sa.Text(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['garage_id'], ['garage.id'], name=op.f('fk_garage_map_shape_garage_id_garage'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_map_shape')),
    )
    with op.batch_alter_table('garage_map_shape', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_garage_map_shape_garage_id'), ['garage_id'], unique=True)

    op.create_table(
        'garage_map_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('height', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('background_file', sa.String(length=500), nullable=True),
        sa.Column('background_opacity', sa.Integer(), nullable=False, server_default='50'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_map_settings')),
    )


def downgrade() -> None:
    op.drop_table('garage_map_settings')
    with op.batch_alter_table('garage_map_shape', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_map_shape_garage_id'))
    op.drop_table('garage_map_shape')
