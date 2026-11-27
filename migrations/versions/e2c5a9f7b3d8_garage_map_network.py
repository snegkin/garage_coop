"""Схема территории: сетевое оборудование (модель, IP, MAC, назначение, подключение, вики, POWCT) и файлы бэкапов

Revision ID: e2c5a9f7b3d8
Revises: d4a7e2b9c6f1
Create Date: 2026-11-27 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2c5a9f7b3d8'
down_revision: Union[str, Sequence[str], None] = 'd4a7e2b9c6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('hostname', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('ip_address', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('mac', sa.String(length=17), nullable=True))
        batch_op.add_column(sa.Column('net_role', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('uplink_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('wiki_page_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('power_device_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_garage_map_object_power_device_id'), ['power_device_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_garage_map_object_power_device_id_power_phase_device'),
                                    'power_phase_device', ['power_device_id'], ['id'], ondelete='SET NULL')
        batch_op.create_index(batch_op.f('ix_garage_map_object_uplink_id'), ['uplink_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_garage_map_object_wiki_page_id'), ['wiki_page_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_garage_map_object_uplink_id_garage_map_object'),
                                    'garage_map_object', ['uplink_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key(batch_op.f('fk_garage_map_object_wiki_page_id_wiki_page'),
                                    'wiki_page', ['wiki_page_id'], ['id'], ondelete='SET NULL')

    op.create_table(
        'garage_map_object_file',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('object_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('original_name', sa.String(length=255), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['object_id'], ['garage_map_object.id'],
                                name=op.f('fk_garage_map_object_file_object_id_garage_map_object'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_map_object_file')),
    )
    with op.batch_alter_table('garage_map_object_file', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_garage_map_object_file_object_id'), ['object_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('garage_map_object_file', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_map_object_file_object_id'))
    op.drop_table('garage_map_object_file')
    with op.batch_alter_table('garage_map_object', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_garage_map_object_power_device_id_power_phase_device'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_garage_map_object_wiki_page_id_wiki_page'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_garage_map_object_power_device_id'))
        batch_op.drop_constraint(batch_op.f('fk_garage_map_object_uplink_id_garage_map_object'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_garage_map_object_wiki_page_id'))
        batch_op.drop_index(batch_op.f('ix_garage_map_object_uplink_id'))
        for col in ('power_device_id', 'wiki_page_id', 'uplink_id', 'net_role', 'mac', 'ip_address', 'hostname', 'model'):
            batch_op.drop_column(col)
