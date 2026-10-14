"""add user pd consent at

Согласие на обработку персональных данных при входе (152-ФЗ) — когда дано.
У существующих учётных записей пусто: согласие запросится при следующем входе.

Revision ID: b5d1f3a8c720
Revises: a3c9e5f7b214
Create Date: 2026-10-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5d1f3a8c720'
down_revision: Union[str, Sequence[str], None] = 'a3c9e5f7b214'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pd_consent_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('pd_consent_at')
