"""add land privatization date to garage

Revision ID: 53476a1af6cf
Revises: ac070ea03392
Create Date: 2026-09-05 13:04:23.134953

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '53476a1af6cf'
down_revision: Union[str, Sequence[str], None] = 'ac070ea03392'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    'cooperative.garage_area' исключён из автогенерации — несвязанный с
    этой миграцией дрейф схемы (колонки уже нет в models.py, но она
    осталась в БД с более ранней миграции), не в рамках этой задачи.
    """
    with op.batch_alter_table('garage', schema=None) as batch_op:
        batch_op.add_column(sa.Column('land_privatization_date', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('garage', schema=None) as batch_op:
        batch_op.drop_column('land_privatization_date')
