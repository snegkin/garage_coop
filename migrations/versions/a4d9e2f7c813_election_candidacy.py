"""Самовыдвижение в правление, ревизионную комиссию, председатели

Revision ID: a4d9e2f7c813
Revises: e7b4c1d9a265
Create Date: 2026-11-19 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4d9e2f7c813'
down_revision: Union[str, Sequence[str], None] = 'e7b4c1d9a265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'election_candidacy',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Enum('BOARD', 'REVISION_COMMISSION', 'CHAIRMAN', name='electionposition'), nullable=False),
        sa.Column('statement', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_election_candidacy_person_id_person'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_election_candidacy')),
        sa.UniqueConstraint('person_id', 'position', name='uq_election_candidacy_person_position'),
    )
    with op.batch_alter_table('election_candidacy', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_election_candidacy_person_id'), ['person_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_election_candidacy_position'), ['position'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('election_candidacy', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_election_candidacy_position'))
        batch_op.drop_index(batch_op.f('ix_election_candidacy_person_id'))
    op.drop_table('election_candidacy')
