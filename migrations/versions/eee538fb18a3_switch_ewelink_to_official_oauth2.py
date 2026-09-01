"""switch ewelink to official oauth2

Revision ID: eee538fb18a3
Revises: 878d06ce54f2
Create Date: 2026-09-01 10:42:46.269616

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eee538fb18a3'
down_revision: Union[str, Sequence[str], None] = '878d06ce54f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('ewelink_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('family_id', sa.String(length=64), nullable=True))
        batch_op.drop_column('email')
        batch_op.drop_column('password_encrypted')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ewelink_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('email', sa.String(length=255), nullable=True))
        batch_op.drop_column('family_id')
