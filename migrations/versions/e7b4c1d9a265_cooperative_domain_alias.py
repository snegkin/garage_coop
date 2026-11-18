"""Кооператив: второй домен сайта/почты и что показывать в документах

Revision ID: e7b4c1d9a265
Revises: d5a8f2c6e391
Create Date: 2026-11-18 19:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b4c1d9a265'
down_revision: Union[str, Sequence[str], None] = 'd5a8f2c6e391'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.add_column(sa.Column('domain_alias', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('site_display', sa.String(8), nullable=False, server_default='main'))
        batch_op.add_column(sa.Column('email_display', sa.String(8), nullable=False, server_default='main'))


def downgrade() -> None:
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.drop_column('email_display')
        batch_op.drop_column('site_display')
        batch_op.drop_column('domain_alias')
