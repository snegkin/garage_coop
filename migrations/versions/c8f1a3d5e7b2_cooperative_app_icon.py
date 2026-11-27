"""Иконка сайта и PWA-приложения кооператива (cooperative.app_icon_png)

Revision ID: c8f1a3d5e7b2
Revises: b6d2e8f4a1c9
Create Date: 2026-11-27 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8f1a3d5e7b2'
down_revision: Union[str, Sequence[str], None] = 'b6d2e8f4a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.add_column(sa.Column('app_icon_png', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('app_icon_updated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.drop_column('app_icon_updated_at')
        batch_op.drop_column('app_icon_png')
