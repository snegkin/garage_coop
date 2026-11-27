"""Схема территории: видеорегистраторы и камеры (привязка к dvr_recorder / dvr_camera)

Revision ID: f7b1d3e9a2c4
Revises: e2c5a9f7b3d8
Create Date: 2026-11-27 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7b1d3e9a2c4'
down_revision: Union[str, Sequence[str], None] = 'e2c5a9f7b3d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dvr_recorder_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('dvr_camera_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_garage_map_object_dvr_recorder_id'), ['dvr_recorder_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_garage_map_object_dvr_camera_id'), ['dvr_camera_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_garage_map_object_dvr_recorder_id_dvr_recorder'),
                                    'dvr_recorder', ['dvr_recorder_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key(batch_op.f('fk_garage_map_object_dvr_camera_id_dvr_camera'),
                                    'dvr_camera', ['dvr_camera_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_garage_map_object_dvr_camera_id_dvr_camera'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_garage_map_object_dvr_recorder_id_dvr_recorder'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_garage_map_object_dvr_camera_id'))
        batch_op.drop_index(batch_op.f('ix_garage_map_object_dvr_recorder_id'))
        batch_op.drop_column('dvr_camera_id')
        batch_op.drop_column('dvr_recorder_id')
