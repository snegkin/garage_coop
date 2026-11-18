"""Вход через Яндекс ID и VK ID

Revision ID: b9e3d7a1c428
Revises: c4e8b2a6d917
Create Date: 2026-11-18 16:00:00

external_login_settings — реквизиты приложений Яндекс ID и VK ID
(единственная запись), external_identity — привязки учётных записей к
аккаунтам у провайдеров (см. app/external_login.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b9e3d7a1c428'
down_revision: Union[str, Sequence[str], None] = 'c4e8b2a6d917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_login_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("yandex_client_id", sa.String(64), nullable=True),
        sa.Column("yandex_client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("vk_client_id", sa.String(32), nullable=True),
    )
    op.create_table(
        "external_identity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("provider", "subject", name="uq_external_identity_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_external_identity_user_provider"),
    )
    op.create_index("ix_external_identity_user_id", "external_identity", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_external_identity_user_id", table_name="external_identity")
    op.drop_table("external_identity")
    op.drop_table("external_login_settings")
