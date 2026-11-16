"""Самопотребление счётчиков за территорией; договор по заявлению на подключение

Revision ID: b5e1c8d3a702
Revises: e2a8b4d6f193
Create Date: 2026-11-16 18:00:00

ElectricitySettings.meter_self_consumption_w — мощность самопотребления
счётчика, Вт (по умолчанию 2); ElectricityReading.meter_self_consumption —
сколько кВт·ч прибавлено к дельте при начислении.

Стандартному «Заявлению на подключение к электрической сети» (по названию)
— метка code=power_application: по таким заявлениям правление оформляет
договор (documents.application_document_code).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5e1c8d3a702'
down_revision: Union[str, Sequence[str], None] = 'e2a8b4d6f193'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("electricity_settings") as batch:
        batch.add_column(sa.Column("meter_self_consumption_w", sa.Numeric(5, 2), nullable=False, server_default="2"))
    with op.batch_alter_table("electricity_reading") as batch:
        batch.add_column(sa.Column("meter_self_consumption", sa.Numeric(12, 3), nullable=True))
    op.get_bind().execute(sa.text(
        "UPDATE document_template SET code = 'power_application' "
        "WHERE name = 'Заявление на подключение к электрической сети' AND code IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM document_template WHERE code = 'power_application')"
    ))


def downgrade() -> None:
    op.get_bind().execute(sa.text("UPDATE document_template SET code = NULL WHERE code = 'power_application'"))
    with op.batch_alter_table("electricity_reading") as batch:
        batch.drop_column("meter_self_consumption")
    with op.batch_alter_table("electricity_settings") as batch:
        batch.drop_column("meter_self_consumption_w")
