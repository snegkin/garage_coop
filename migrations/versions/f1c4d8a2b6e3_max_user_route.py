"""Один бот MAX на все кооперативы сервера: в каком кооперативе привязан аккаунт (MaxUserRoute)

Revision ID: f1c4d8a2b6e3
Revises: e5b9c3a1d7f2
Create Date: 2026-11-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1c4d8a2b6e3"
down_revision: Union[str, Sequence[str], None] = "e5b9c3a1d7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "max_user_route",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("coop_code", sa.String(64), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("max_user_id", "coop_code", name="uq_max_user_route"),
    )
    op.create_index("ix_max_user_route_max_user_id", "max_user_route", ["max_user_id"])


def downgrade() -> None:
    op.drop_table("max_user_route")
