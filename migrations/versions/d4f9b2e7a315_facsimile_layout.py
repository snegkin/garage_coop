"""Расположение факсимиле на бланке: cooperative.seal_layout, person.signature_layout

Revision ID: d4f9b2e7a315
Revises: c1a8d3f6e724
Create Date: 2026-11-14 14:00:00

JSON со сдвигами и размером в мм (app/facsimile.py: SIGN_LAYOUT, SEAL_LAYOUT).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4f9b2e7a315'
down_revision: Union[str, Sequence[str], None] = 'c1a8d3f6e724'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cooperative') as batch_op:
        batch_op.add_column(sa.Column('seal_layout', sa.String(length=200), nullable=True))
    with op.batch_alter_table('person') as batch_op:
        batch_op.add_column(sa.Column('signature_layout', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('person') as batch_op:
        batch_op.drop_column('signature_layout')
    with op.batch_alter_table('cooperative') as batch_op:
        batch_op.drop_column('seal_layout')
