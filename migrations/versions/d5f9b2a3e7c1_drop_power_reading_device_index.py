"""drop power reading device index

ix_power_phase_reading_device_id дублирует левую часть составного
ix_power_phase_reading_device_ts — SQLite использует составной и для
фильтра только по device_id; лишний индекс занимал ~1,7 МБ и замедлял
вставку поминутных показаний.

Revision ID: d5f9b2a3e7c1
Revises: c4e8a1f2d6b9
Create Date: 2026-10-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd5f9b2a3e7c1'
down_revision: Union[str, Sequence[str], None] = 'c4e8a1f2d6b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index('ix_power_phase_reading_device_id', table_name='power_phase_reading')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index('ix_power_phase_reading_device_id', 'power_phase_reading', ['device_id'], unique=False)
