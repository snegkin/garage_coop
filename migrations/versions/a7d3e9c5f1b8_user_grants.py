"""Технические разделы, доверенные председателем члену правления (User.grants)

Revision ID: a7d3e9c5f1b8
Revises: f1c4d8a2b6e3
Create Date: 2026-11-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e9c5f1b8"
down_revision: Union[str, Sequence[str], None] = "f1c4d8a2b6e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("grants", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.drop_column("grants")
