"""garage certificates

Журнал справок правления о владении гаражом (гаражная амнистия) и счётчик
их номеров по годам (см. app/models.py: GarageCertificate, app/certificates.py).

Revision ID: d918ca37bb70
Revises: df12e3b5f48d
Create Date: 2026-10-20 18:58:48.531581

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd918ca37bb70'
down_revision: Union[str, Sequence[str], None] = 'df12e3b5f48d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('certificate_counter',
    sa.Column('year', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('last_number', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('year', name=op.f('pk_certificate_counter'))
    )
    op.create_table('garage_certificate',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=30), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('garage_id', sa.Integer(), nullable=True),
    sa.Column('garage_number', sa.String(length=20), nullable=False),
    sa.Column('body_html', sa.Text(), nullable=False),
    sa.Column('issued_at', sa.Date(), nullable=False),
    sa.Column('issued_by_person_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['garage_id'], ['garage.id'], name=op.f('fk_garage_certificate_garage_id_garage'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['issued_by_person_id'], ['person.id'], name=op.f('fk_garage_certificate_issued_by_person_id_person'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_garage_certificate_person_id_person'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_certificate')),
    sa.UniqueConstraint('number', name=op.f('uq_garage_certificate_number'))
    )
    with op.batch_alter_table('garage_certificate', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_garage_certificate_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_garage_certificate_issued_at'), ['issued_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_garage_certificate_person_id'), ['person_id'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('garage_certificate', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_garage_certificate_person_id'))
        batch_op.drop_index(batch_op.f('ix_garage_certificate_issued_at'))
        batch_op.drop_index(batch_op.f('ix_garage_certificate_garage_id'))

    op.drop_table('garage_certificate')
    op.drop_table('certificate_counter')
