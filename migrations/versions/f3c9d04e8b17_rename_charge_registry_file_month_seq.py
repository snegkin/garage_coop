"""rename charge registry file month seq to day seq

Порядковый номер в имени файла реестра начислений — за день, а не за
месяц (ИНН_расчётный-счёт_номер-за-день_дд.мм.гггг.TXT): month_seq ->
day_seq. Уже сохранённые значения переносятся как есть.

Revision ID: f3c9d04e8b17
Revises: e2b7a91c5d36
Create Date: 2026-09-29 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f3c9d04e8b17'
down_revision: Union[str, Sequence[str], None] = 'e2b7a91c5d36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('charge_registry_file', schema=None) as batch_op:
        batch_op.alter_column('month_seq', new_column_name='day_seq')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('charge_registry_file', schema=None) as batch_op:
        batch_op.alter_column('day_seq', new_column_name='month_seq')
