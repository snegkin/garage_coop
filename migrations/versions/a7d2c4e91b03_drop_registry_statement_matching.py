"""drop registry/statement matching

Сопоставление записей реестра платежей со строками выписки один к одному
(matched_registry_id / matched_statement_id) ни разу не сработало: деньги
по реестру приходят в выписку одной сводной строкой «по принятым
платежам» на следующий день и за вычетом комиссии, без лицевого счёта и
со своим uuid — ни прямое (по ID), ни параметрическое (л/с + сумма + дата)
сопоставление на реальных данных невозможно. Колонки убраны вместе с
логикой; обратная миграция возвращает их пустыми.

Revision ID: a7d2c4e91b03
Revises: f3c9d04e8b17
Create Date: 2026-10-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d2c4e91b03'
down_revision: Union[str, Sequence[str], None] = 'f3c9d04e8b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('payment_registry_entry', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_payment_registry_entry_matched_statement_id_bank_statement_line'), type_='foreignkey')
        batch_op.drop_column('matched_statement_id')

    with op.batch_alter_table('bank_statement_line', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_bank_statement_line_matched_registry_id_payment_registry_entry'), type_='foreignkey')
        batch_op.drop_column('matched_registry_id')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('bank_statement_line', schema=None) as batch_op:
        batch_op.add_column(sa.Column('matched_registry_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_bank_statement_line_matched_registry_id_payment_registry_entry'),
            'payment_registry_entry', ['matched_registry_id'], ['id'], ondelete='SET NULL'
        )

    with op.batch_alter_table('payment_registry_entry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('matched_statement_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_payment_registry_entry_matched_statement_id_bank_statement_line'),
            'bank_statement_line', ['matched_statement_id'], ['id'], ondelete='SET NULL'
        )
