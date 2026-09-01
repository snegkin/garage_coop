"""archive member accounts on ownership transfer

Revision ID: 5300dca34cc6
Revises: 55f9d7b0def0
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5300dca34cc6'
down_revision: Union[str, Sequence[str], None] = '55f9d7b0def0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('member_account', schema=None) as batch_op:
        # server_default нужен только на время добавления колонки — у уже
        # существующих счетов is_archived нигде раньше не задавался,
        # безопасное значение по умолчанию — False (активен), сохраняет
        # прежнее поведение.
        batch_op.add_column(sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.drop_constraint(batch_op.f('uq_member_account_account_number'), type_='unique')
        batch_op.drop_constraint(batch_op.f('uq_member_account'), type_='unique')
    with op.batch_alter_table('member_account', schema=None) as batch_op:
        batch_op.alter_column('is_archived', server_default=None)

    # Уникальность номера счёта и уникальность (человек, гараж, вид взноса)
    # теперь только СРЕДИ АКТИВНЫХ счетов — у архивного счёта (прежний
    # собственник выбыл) номер переходит новому, активному счёту (см.
    # garages._archive_owner_accounts_and_reuse), а сам архивный, случись
    # тому же человеку однажды вернуться в собственники, не должен мешать
    # завести ему новый активный счёт (см. app/garages.py).
    op.create_index(
        'uq_member_account_active', 'member_account', ['person_id', 'garage_id', 'fee_type_id'],
        unique=True, sqlite_where=sa.text('NOT is_archived'),
    )
    op.create_index(
        'uq_member_account_number_active', 'member_account', ['account_number'],
        unique=True, sqlite_where=sa.text('NOT is_archived'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_member_account_number_active', table_name='member_account')
    op.drop_index('uq_member_account_active', table_name='member_account')

    with op.batch_alter_table('member_account', schema=None) as batch_op:
        batch_op.create_unique_constraint(batch_op.f('uq_member_account'), ['person_id', 'garage_id', 'fee_type_id'])
        batch_op.create_unique_constraint(batch_op.f('uq_member_account_account_number'), ['account_number'])
        batch_op.drop_column('is_archived')
