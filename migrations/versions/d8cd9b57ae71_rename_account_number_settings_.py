"""rename account_number_settings electricity_prefix to type_code

Чистое переименование поля, без изменения смысла — код типа для счёта на
электричество теперь называется как и одноимённое поле FeeType.type_code
у видов взноса (см. её докстринг в models.py), по прямой просьбе, для
единообразия терминологии (оба смысла теперь можно редактировать в одной
таблице на /finance/account-format).

Revision ID: d8cd9b57ae71
Revises: 4aafd6b5140a
Create Date: 2026-09-15 10:45:10.501528

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8cd9b57ae71'
down_revision: Union[str, Sequence[str], None] = '4aafd6b5140a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('account_number_settings', schema=None) as batch_op:
        batch_op.alter_column(
            'electricity_prefix', new_column_name='type_code', existing_type=sa.String(length=10),
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('account_number_settings', schema=None) as batch_op:
        batch_op.alter_column(
            'type_code', new_column_name='electricity_prefix', existing_type=sa.String(length=10),
        )
