"""Записи правления без сети (FieldEntry, app/offline_sync.py)

Revision ID: e5b9c3a1d7f2
Revises: d4a8e2f7b9c1
Create Date: 2026-11-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b9c3a1d7f2"
down_revision: Union[str, Sequence[str], None] = "d4a8e2f7b9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_entry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("photo_path", sa.String(500), nullable=True),
        sa.Column("receipt_number", sa.String(30), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(30), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_field_entry_uuid", "field_entry", ["uuid"], unique=True)
    op.create_index("ix_field_entry_status", "field_entry", ["status"])
    op.create_index("ix_field_entry_user_id", "field_entry", ["user_id"])
    op.create_index("ix_field_entry_received_at", "field_entry", ["received_at"])
    op.create_index("ix_field_entry_receipt_number", "field_entry", ["receipt_number"])


def downgrade() -> None:
    op.drop_table("field_entry")
