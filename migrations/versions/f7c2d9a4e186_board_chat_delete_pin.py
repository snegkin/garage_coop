"""Чат правления: удаление и закреп сообщений, связь с очередью мессенджеров

Revision ID: f7c2d9a4e186
Revises: e5b8c1f3a947
Create Date: 2026-11-13 19:00:00

BoardChatMessage: deleted_at/deleted_by_id, pinned_at/pinned_by_id.
MessengerOutbox: board_message_id, external_id — id копии в группе
мессенджера, по нему удаление и закреп повторяются там (app/board_chat.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7c2d9a4e186'
down_revision: Union[str, Sequence[str], None] = 'e5b8c1f3a947'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('deleted_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('pinned_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('pinned_by_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_board_chat_message_deleted_at'), ['deleted_at'])
        batch_op.create_foreign_key(batch_op.f('fk_board_chat_message_deleted_by_id_user'), 'user',
                                    ['deleted_by_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key(batch_op.f('fk_board_chat_message_pinned_by_id_user'), 'user',
                                    ['pinned_by_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('messenger_outbox') as batch_op:
        batch_op.add_column(sa.Column('board_message_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('external_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_messenger_outbox_board_message_id'), ['board_message_id'])
        batch_op.create_foreign_key(batch_op.f('fk_messenger_outbox_board_message_id_board_chat_message'),
                                    'board_chat_message', ['board_message_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('messenger_outbox') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_messenger_outbox_board_message_id_board_chat_message'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_messenger_outbox_board_message_id'))
        batch_op.drop_column('external_id')
        batch_op.drop_column('board_message_id')
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_board_chat_message_pinned_by_id_user'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_board_chat_message_deleted_by_id_user'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_board_chat_message_deleted_at'))
        batch_op.drop_column('pinned_by_id')
        batch_op.drop_column('pinned_at')
        batch_op.drop_column('deleted_by_id')
        batch_op.drop_column('deleted_at')
