"""certificate requests

Запросы справки о владении гаражом из личного кабинета собственника
(см. app/models.py: CertificateRequest, app/certificates.py).

Revision ID: 276efa571cb9
Revises: d918ca37bb70
Create Date: 2026-10-20 19:05:05.654807

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '276efa571cb9'
down_revision: Union[str, Sequence[str], None] = 'd918ca37bb70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('certificate_request',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('garage_id', sa.Integer(), nullable=True),
    sa.Column('garage_number', sa.String(length=20), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('REQUESTED', 'READY', 'HANDED_OVER', 'REJECTED', name='certificaterequeststatus'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('certificate_id', sa.Integer(), nullable=True),
    sa.Column('ready_at', sa.DateTime(), nullable=True),
    sa.Column('handed_over_at', sa.DateTime(), nullable=True),
    sa.Column('reject_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['certificate_id'], ['garage_certificate.id'], name=op.f('fk_certificate_request_certificate_id_garage_certificate'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['garage_id'], ['garage.id'], name=op.f('fk_certificate_request_garage_id_garage'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_certificate_request_person_id_person'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_certificate_request'))
    )
    with op.batch_alter_table('certificate_request', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_certificate_request_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_certificate_request_person_id'), ['person_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_certificate_request_status'), ['status'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('certificate_request', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_certificate_request_status'))
        batch_op.drop_index(batch_op.f('ix_certificate_request_person_id'))
        batch_op.drop_index(batch_op.f('ix_certificate_request_garage_id'))

    op.drop_table('certificate_request')
