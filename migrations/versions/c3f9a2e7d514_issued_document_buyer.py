"""issued document buyer

IssuedDocument.buyer_person_id — покупатель по договору купли-продажи
гаража (шаблон «sale», app/documents.py): продавец — person_id.

Revision ID: c3f9a2e7d514
Revises: b7e3f1a9c264
Create Date: 2026-11-17 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3f9a2e7d514'
down_revision: Union[str, Sequence[str], None] = 'b7e3f1a9c264'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('issued_document') as batch_op:
        batch_op.add_column(sa.Column('buyer_person_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_issued_document_buyer_person_id'), ['buyer_person_id'])
        batch_op.create_foreign_key(batch_op.f('fk_issued_document_buyer_person_id_person'), 'person',
                                    ['buyer_person_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('issued_document') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_issued_document_buyer_person_id_person'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_issued_document_buyer_person_id'))
        batch_op.drop_column('buyer_person_id')
