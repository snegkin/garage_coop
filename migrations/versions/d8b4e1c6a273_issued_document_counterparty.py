"""issued document counterparty

IssuedDocument.counterparty_id — письмо контрагенту на бланке (шаблоны
с recipient="contractor", app/documents.py), видно в карточке контрагента.

Revision ID: d8b4e1c6a273
Revises: c3f9a2e7d514
Create Date: 2026-11-17 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8b4e1c6a273'
down_revision: Union[str, Sequence[str], None] = 'c3f9a2e7d514'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('issued_document') as batch_op:
        batch_op.add_column(sa.Column('counterparty_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_issued_document_counterparty_id'), ['counterparty_id'])
        batch_op.create_foreign_key(batch_op.f('fk_issued_document_counterparty_id_counterparty'), 'counterparty',
                                    ['counterparty_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('issued_document') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_issued_document_counterparty_id_counterparty'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_issued_document_counterparty_id'))
        batch_op.drop_column('counterparty_id')
