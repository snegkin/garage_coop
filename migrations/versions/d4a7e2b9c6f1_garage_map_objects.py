"""Схема территории: объекты (щиты, фонари, счётчики, рубильники, отметки) и их фото

Revision ID: d4a7e2b9c6f1
Revises: c8f1a3d5e7b2
Create Date: 2026-11-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4a7e2b9c6f1'
down_revision: Union[str, Sequence[str], None] = 'c8f1a3d5e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'garage_map_object',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='mark'),
        sa.Column('x', sa.Float(), nullable=False, server_default='0'),
        sa.Column('y', sa.Float(), nullable=False, server_default='0'),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('master', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('control_meter_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['control_meter_id'], ['control_meter.id'],
                                name=op.f('fk_garage_map_object_control_meter_id_control_meter'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_map_object')),
    )
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_garage_map_object_control_meter_id'), ['control_meter_id'], unique=False)

    op.create_table(
        'garage_map_object_photo',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('object_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('caption', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['object_id'], ['garage_map_object.id'],
                                name=op.f('fk_garage_map_object_photo_object_id_garage_map_object'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_map_object_photo')),
    )
    with op.batch_alter_table('garage_map_object_photo', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_garage_map_object_photo_object_id'), ['object_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('garage_map_object_photo', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_map_object_photo_object_id'))
    op.drop_table('garage_map_object_photo')
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_map_object_control_meter_id'))
    op.drop_table('garage_map_object')
