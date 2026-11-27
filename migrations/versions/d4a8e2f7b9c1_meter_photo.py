"""meter_photo: фото счётчика к показаниям

Показания по фото (app/meter_photos.py): снимок, что нашло
распознавание и к каким показаниям он относится. Новая таблица, без
бэкофилла.

Revision ID: d4a8e2f7b9c1
Revises: c7f2a9d4e1b6
Create Date: 2026-11-27 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4a8e2f7b9c1'
down_revision: Union[str, Sequence[str], None] = 'c7f2a9d4e1b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('meter_photo',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('uploaded_by_user_id', sa.Integer(), nullable=True),
    sa.Column('file_path', sa.String(length=500), nullable=False),
    sa.Column('garage_id', sa.Integer(), nullable=True),
    sa.Column('meter_id', sa.Integer(), nullable=True),
    sa.Column('reading_id', sa.Integer(), nullable=True),
    sa.Column('ocr_tokens', sa.Text(), nullable=True),
    sa.Column('ocr_error', sa.Text(), nullable=True),
    sa.Column('suggested_meter_number', sa.String(length=50), nullable=True),
    sa.Column('suggested_reading', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('taken_date', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['uploaded_by_user_id'], ['user.id'], name=op.f('fk_meter_photo_uploaded_by_user_id_user'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['garage_id'], ['garage.id'], name=op.f('fk_meter_photo_garage_id_garage'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['meter_id'], ['electricity_meter.id'], name=op.f('fk_meter_photo_meter_id_electricity_meter'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reading_id'], ['electricity_reading.id'], name=op.f('fk_meter_photo_reading_id_electricity_reading'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meter_photo'))
    )
    with op.batch_alter_table('meter_photo', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_meter_photo_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_meter_photo_uploaded_by_user_id'), ['uploaded_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_meter_photo_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_meter_photo_meter_id'), ['meter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_meter_photo_reading_id'), ['reading_id'], unique=True)


def downgrade() -> None:
    op.drop_table('meter_photo')
