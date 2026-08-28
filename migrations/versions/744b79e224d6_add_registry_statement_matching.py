"""add_registry_statement_matching

Revision ID: 744b79e224d6
Revises: 4deb675b904f
Create Date: 2026-08-28 09:34:24.395746

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '744b79e224d6'
down_revision: Union[str, Sequence[str], None] = '4deb675b904f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add matched_registry_id to bank_statement_line
    with op.batch_alter_table('bank_statement_line', schema=None) as batch_op:
        batch_op.add_column(sa.Column('matched_registry_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_bank_statement_line_matched_registry_id_payment_registry_entry'),
            'payment_registry_entry', ['matched_registry_id'], ['id'], ondelete='SET NULL'
        )

    # Add matched_statement_id to payment_registry_entry
    with op.batch_alter_table('payment_registry_entry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('matched_statement_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_payment_registry_entry_matched_statement_id_bank_statement_line'),
            'bank_statement_line', ['matched_statement_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('payment_registry_entry', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_payment_registry_entry_matched_statement_id_bank_statement_line'), type_='foreignkey')
        batch_op.drop_column('matched_statement_id')

    with op.batch_alter_table('bank_statement_line', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_bank_statement_line_matched_registry_id_payment_registry_entry'), type_='foreignkey')
        batch_op.drop_column('matched_registry_id')
