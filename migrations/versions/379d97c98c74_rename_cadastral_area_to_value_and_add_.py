"""rename_cadastral_area_to_value_and_add_cadastral_area

Revision ID: 379d97c98c74
Revises: 9f9c21a5405c
Create Date: 2026-08-29 22:08:37.481980

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '379d97c98c74'
down_revision: Union[str, Sequence[str], None] = '9f9c21a5405c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cadastral_value', sa.Numeric(precision=14, scale=2), nullable=True))
        batch_op.alter_column('cadastral_area',
               existing_type=sa.NUMERIC(precision=14, scale=2),
               type_=sa.Numeric(precision=12, scale=2),
               existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.drop_column('cadastral_value')
        batch_op.alter_column('cadastral_area',
               existing_type=sa.Numeric(precision=12, scale=2),
               type_=sa.NUMERIC(precision=14, scale=2),
               existing_nullable=True)
