"""widen counterparty phone field

Revision ID: 2a6e40f47ad9
Revises: dfb026828053
Create Date: 2026-09-05 00:02:11.661618

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a6e40f47ad9'
down_revision: Union[str, Sequence[str], None] = 'dfb026828053'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    'cooperative.garage_area' исключён из автогенерации — несвязанный с
    этой миграцией дрейф схемы (колонки уже нет в models.py, но она
    осталась в БД с более ранней миграции), не в рамках этой задачи.
    """
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.alter_column('phone',
               existing_type=sa.VARCHAR(length=30),
               type_=sa.String(length=120),
               existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.alter_column('phone',
               existing_type=sa.String(length=120),
               type_=sa.VARCHAR(length=30),
               existing_nullable=True)
