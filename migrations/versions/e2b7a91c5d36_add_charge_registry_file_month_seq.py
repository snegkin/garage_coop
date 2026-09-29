"""add charge registry file month seq

charge_registry_file.file_date/month_seq — дата и порядковый номер файла в
месяце для имени файла по требованию банка:
ИНН_расчётный-счёт_номер-в-месяце_дд.мм.гггг.TXT (см. models.ChargeRegistryFile).

Revision ID: e2b7a91c5d36
Revises: c4d81f6e2a90
Create Date: 2026-09-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b7a91c5d36'
down_revision: Union[str, Sequence[str], None] = 'c4d81f6e2a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('charge_registry_file', schema=None) as batch_op:
        batch_op.add_column(sa.Column('file_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('month_seq', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('charge_registry_file', schema=None) as batch_op:
        batch_op.drop_column('month_seq')
        batch_op.drop_column('file_date')
