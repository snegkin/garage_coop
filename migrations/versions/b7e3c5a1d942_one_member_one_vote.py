"""Режим голосования «один член — один голос» (настройка кооператива и голосования)

Revision ID: b7e3c5a1d942
Revises: a4d9e2f7c813
Create Date: 2026-11-19 14:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e3c5a1d942'
down_revision: Union[str, Sequence[str], None] = 'a4d9e2f7c813'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.add_column(sa.Column('one_member_one_vote', sa.Boolean(), nullable=False, server_default='0'))
    with op.batch_alter_table('vote', schema=None) as batch_op:
        batch_op.add_column(sa.Column('one_member_one_vote', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('vote', schema=None) as batch_op:
        batch_op.drop_column('one_member_one_vote')
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.drop_column('one_member_one_vote')
