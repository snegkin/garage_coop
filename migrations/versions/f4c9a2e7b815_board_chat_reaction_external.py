"""board chat reaction from messengers

BoardChatReaction: user_id nullable, source, external_user_id, external_name —
реакции из группы правления в Telegram (апдейт message_reaction), см.
app/board_chat.py: ingest_external_reaction.

Revision ID: f4c9a2e7b815
Revises: b5e1c8d3a702
Create Date: 2026-11-16 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4c9a2e7b815'
down_revision: Union[str, Sequence[str], None] = 'b5e1c8d3a702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('board_chat_reaction') as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='site', nullable=False))
        batch_op.add_column(sa.Column('external_user_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('external_name', sa.String(length=128), nullable=True))
        batch_op.create_unique_constraint(
            'uq_board_chat_reaction_external', ['message_id', 'source', 'external_user_id', 'emoji'])


def downgrade() -> None:
    op.execute("DELETE FROM board_chat_reaction WHERE user_id IS NULL")
    with op.batch_alter_table('board_chat_reaction') as batch_op:
        batch_op.drop_constraint('uq_board_chat_reaction_external', type_='unique')
        batch_op.drop_column('external_name')
        batch_op.drop_column('external_user_id')
        batch_op.drop_column('source')
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
