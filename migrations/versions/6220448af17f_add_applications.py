"""add applications

Заявления членов кооператива правлению на бланке (см. app/models.py:
Application, app/applications.py) — отметки «ознакомлен» членов правления
и счётчик номеров заявлений по годам («2026-1»).

Revision ID: 6220448af17f
Revises: e8a1c5d3f720
Create Date: 2026-10-20 18:12:49.310003

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6220448af17f'
down_revision: Union[str, Sequence[str], None] = 'e8a1c5d3f720'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('application_counter',
    sa.Column('year', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('last_number', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('year', name=op.f('pk_application_counter'))
    )
    op.create_table('application',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=30), nullable=False),
    sa.Column('kind', sa.Enum('OWNERSHIP_CHANGE', 'CLAIM', 'OTHER', name='applicationkind'), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('body_html', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('SUBMITTED', 'ANSWERED', 'REJECTED', name='applicationstatus'), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('garage_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('applicant_name', sa.String(length=255), nullable=False),
    sa.Column('applicant_address', sa.Text(), nullable=True),
    sa.Column('applicant_passport', sa.String(length=255), nullable=True),
    sa.Column('applicant_phone', sa.String(length=255), nullable=True),
    sa.Column('garage_number', sa.String(length=20), nullable=True),
    sa.Column('response_html', sa.Text(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('decided_by_person_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['decided_by_person_id'], ['person.id'], name=op.f('fk_application_decided_by_person_id_person'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['garage_id'], ['garage.id'], name=op.f('fk_application_garage_id_garage'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_application_person_id_person'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_application')),
    sa.UniqueConstraint('number', name=op.f('uq_application_number'))
    )
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_application_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_application_garage_id'), ['garage_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_application_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_application_person_id'), ['person_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_application_status'), ['status'], unique=False)

    op.create_table('application_read',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('read_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['application.id'], name=op.f('fk_application_read_application_id_application'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_application_read_person_id_person'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_application_read')),
    sa.UniqueConstraint('application_id', 'person_id', name='uq_application_read_application_person')
    )
    with op.batch_alter_table('application_read', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_application_read_application_id'), ['application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_application_read_person_id'), ['person_id'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('application_read', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_application_read_person_id'))
        batch_op.drop_index(batch_op.f('ix_application_read_application_id'))

    op.drop_table('application_read')
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_application_status'))
        batch_op.drop_index(batch_op.f('ix_application_person_id'))
        batch_op.drop_index(batch_op.f('ix_application_kind'))
        batch_op.drop_index(batch_op.f('ix_application_garage_id'))
        batch_op.drop_index(batch_op.f('ix_application_created_at'))

    op.drop_table('application')
    op.drop_table('application_counter')
