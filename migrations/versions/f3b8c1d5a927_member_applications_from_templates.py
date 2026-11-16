"""Заявления в правление по шаблонам «Бланков и шаблонов»

Revision ID: f3b8c1d5a927
Revises: e2c7a9f1b468
Create Date: 2026-11-16 12:00:00

DocumentTemplate.for_members — шаблон можно подать из личного кабинета;
Application.template_id/heading — заявление подано по такому шаблону.
Уже добавленные стандартные заявления о выходе и на подключение к
электросети (по названию) отмечаются for_members.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3b8c1d5a927'
down_revision: Union[str, Sequence[str], None] = 'e2c7a9f1b468'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MEMBER_TEMPLATES = ("Заявление о выходе из кооператива", "Заявление на подключение к электрической сети")


def upgrade() -> None:
    with op.batch_alter_table("document_template") as batch:
        batch.add_column(sa.Column("for_members", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("application") as batch:
        batch.add_column(sa.Column("template_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("heading", sa.String(length=200), nullable=True))
        batch.create_foreign_key(
            "fk_application_template_id_document_template", "document_template",
            ["template_id"], ["id"], ondelete="SET NULL",
        )
    op.get_bind().execute(
        sa.text("UPDATE document_template SET for_members = 1 WHERE name IN (:a, :b)"),
        {"a": MEMBER_TEMPLATES[0], "b": MEMBER_TEMPLATES[1]},
    )


def downgrade() -> None:
    with op.batch_alter_table("application") as batch:
        batch.drop_constraint("fk_application_template_id_document_template", type_="foreignkey")
        batch.drop_column("heading")
        batch.drop_column("template_id")
    with op.batch_alter_table("document_template") as batch:
        batch.drop_column("for_members")
