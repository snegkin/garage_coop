"""rename account_number_settings electricity_prefix to type_code

Чистое переименование поля, без изменения смысла — код типа для счёта на
электричество теперь называется как и одноимённое поле FeeType.type_code
у видов взноса (см. её докстринг в models.py), по прямой просьбе, для
единообразия терминологии (оба смысла теперь можно редактировать в одной
таблице на /finance/account-format).

Заодно меняем сам код по умолчанию с "0" на "9" — по прямой просьбе,
чтобы счета на электричество визуально шли ПОСЛЕДНИМИ среди видов счетов
(в списках, отсортированных по номеру), а не первыми, как раньше с "0".
Затрагивает только ещё не изменённое вручную значение ("0", исходный
дефолт) — если правление уже поменяло код на что-то своё, тот не трогаем.

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
    op.get_bind().execute(sa.text(
        "UPDATE account_number_settings SET type_code = '9' WHERE type_code = '0'"
    ))


def downgrade() -> None:
    """Downgrade schema."""
    op.get_bind().execute(sa.text(
        "UPDATE account_number_settings SET type_code = '0' WHERE type_code = '9'"
    ))
    with op.batch_alter_table('account_number_settings', schema=None) as batch_op:
        batch_op.alter_column(
            'type_code', new_column_name='electricity_prefix', existing_type=sa.String(length=10),
        )
