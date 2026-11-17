"""document is_public

Document.is_public — документ кооператива виден и скачивается без входа
на сайт (устав и т. п.), см. app/cooperative.py: view, download_document.

Revision ID: a6d2f8c4b931
Revises: f4c7b2a9e615
Create Date: 2026-11-17 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a6d2f8c4b931'
down_revision: Union[str, Sequence[str], None] = 'f4c7b2a9e615'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('document') as batch_op:
        batch_op.add_column(sa.Column('is_public', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('document') as batch_op:
        batch_op.drop_column('is_public')
