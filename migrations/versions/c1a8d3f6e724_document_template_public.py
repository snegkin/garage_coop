"""Общедоступные бланки: document_template.is_public

Revision ID: c1a8d3f6e724
Revises: b6e2f40a9d13
Create Date: 2026-11-14 12:00:00

Шаблон с is_public открывается и скачивается без входа (documents.public_*).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1a8d3f6e724'
down_revision: Union[str, Sequence[str], None] = 'b6e2f40a9d13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('document_template') as batch_op:
        batch_op.add_column(sa.Column('is_public', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('document_template') as batch_op:
        batch_op.drop_column('is_public')
