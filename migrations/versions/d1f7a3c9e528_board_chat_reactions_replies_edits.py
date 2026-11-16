"""board chat reactions, replies, edits

Реакции на сообщения чата правления (BoardChatReaction), ответы
(BoardChatMessage.reply_to_id), правка (edited_at) и changed_at — момент
последней правки/реакции для опроса виджета. См. app/board_chat.py.

Revision ID: d1f7a3c9e528
Revises: c8e4a2f6d913
Create Date: 2026-11-16 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1f7a3c9e528'
down_revision: Union[str, Sequence[str], None] = 'c8e4a2f6d913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'board_chat_reaction',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('emoji', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['board_chat_message.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'user_id', 'emoji', name='uq_board_chat_reaction'),
    )
    op.create_index('ix_board_chat_reaction_message_id', 'board_chat_reaction', ['message_id'])
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.add_column(sa.Column('reply_to_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('edited_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('changed_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key('fk_board_chat_message_reply_to', 'board_chat_message',
                                    ['reply_to_id'], ['id'], ondelete='SET NULL')
        batch_op.create_index('ix_board_chat_message_changed_at', ['changed_at'])


def downgrade() -> None:
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.drop_index('ix_board_chat_message_changed_at')
        batch_op.drop_constraint('fk_board_chat_message_reply_to', type_='foreignkey')
        batch_op.drop_column('changed_at')
        batch_op.drop_column('edited_at')
        batch_op.drop_column('reply_to_id')
    op.drop_index('ix_board_chat_reaction_message_id', table_name='board_chat_reaction')
    op.drop_table('board_chat_reaction')
