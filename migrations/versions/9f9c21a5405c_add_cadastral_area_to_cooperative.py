"""add land_tax fields to cooperative

Revision ID: 9f9c21a5405c
Revises: 68135a62e78d
Create Date: 2026-08-29 21:06:47.994231

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f9c21a5405c'
down_revision: Union[str, Sequence[str], None] = '68135a62e78d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cadastral_area', sa.Numeric(precision=14, scale=2), nullable=True))

    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cooperative', schema=None) as batch_op:
        batch_op.drop_column('cadastral_area')

    # ### end Alembic commands ###
