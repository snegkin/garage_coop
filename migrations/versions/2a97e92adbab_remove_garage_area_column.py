"""remove_garage_area_column

Revision ID: 2a97e92adbab
Revises: 744b79e224d6
Create Date: 2026-08-29 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a97e92adbab'
down_revision: Union[str, Sequence[str], None] = '744b79e224d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('cooperative', 'garage_area')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('cooperative', sa.Column('garage_area', sa.Numeric(precision=12, scale=2), nullable=True))
