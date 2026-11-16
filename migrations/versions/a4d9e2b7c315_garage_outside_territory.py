"""Гаражи за территорией кооператива

Revision ID: a4d9e2b7c315
Revises: f3b8c1d5a927
Create Date: 2026-11-16 18:00:00

Garage.outside_territory — гараж за территорией (отдельный договор, не член
кооператива); garage_fee_exclusion — виды взносов, которые такому гаражу
не начисляются; ElectricitySettings.outside_line_loss_percent — потери в
линии до кооператива, % (надбавка к тарифу для таких гаражей).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4d9e2b7c315'
down_revision: Union[str, Sequence[str], None] = 'f3b8c1d5a927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("garage") as batch:
        batch.add_column(sa.Column("outside_territory", sa.Boolean(), nullable=False, server_default="0"))
    with op.batch_alter_table("electricity_settings") as batch:
        batch.add_column(sa.Column("outside_line_loss_percent", sa.Numeric(5, 2), nullable=False, server_default="2.33"))
    op.create_table(
        "garage_fee_exclusion",
        sa.Column("garage_id", sa.Integer(), nullable=False),
        sa.Column("fee_type_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["garage_id"], ["garage.id"], name=op.f("fk_garage_fee_exclusion_garage_id_garage"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fee_type_id"], ["fee_type.id"], name=op.f("fk_garage_fee_exclusion_fee_type_id_fee_type"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("garage_id", "fee_type_id", name=op.f("pk_garage_fee_exclusion")),
    )


def downgrade() -> None:
    op.drop_table("garage_fee_exclusion")
    with op.batch_alter_table("electricity_settings") as batch:
        batch.drop_column("outside_line_loss_percent")
    with op.batch_alter_table("garage") as batch:
        batch.drop_column("outside_territory")
