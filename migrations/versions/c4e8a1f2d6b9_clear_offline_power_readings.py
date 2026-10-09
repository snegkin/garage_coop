"""clear offline power readings

Пока устройство eWeLink офлайн, облако отдаёт его последние известные
показания — поллер сутками записывал одни и те же числа (график — ровная
линия, карточка — «текущая» мощность). Теперь офлайн-отметка пишется без
значений (app/electricity_monitor.py:reading_from_snapshot); здесь так же
очищаются уже накопленные офлайн-записи. Это копии последних онлайн-показаний,
так что обратная миграция ничего не восстанавливает.

Revision ID: c4e8a1f2d6b9
Revises: a7d2c4e91b03
Create Date: 2026-10-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4e8a1f2d6b9'
down_revision: Union[str, Sequence[str], None] = 'a7d2c4e91b03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "UPDATE power_phase_reading SET power_w = NULL, voltage_v = NULL, current_a = NULL,"
        " day_kwh = NULL, month_kwh = NULL, sled_online = NULL, switch_on = NULL"
        " WHERE is_online = 0"
    )


def downgrade() -> None:
    """Downgrade schema."""
