"""Мост чата правления с группами правления в Telegram и MAX

Revision ID: e5b8c1f3a947
Revises: d4a7e2b9c613
Create Date: 2026-11-13 18:00:00

BoardChatMessage: author_id nullable, source, external_author,
external_message_id (+ уникальность по source). TelegramSettings и
MaxSettings: board_chat_id. См. app/board_chat.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5b8c1f3a947'
down_revision: Union[str, Sequence[str], None] = 'd4a7e2b9c613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.alter_column('author_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='site', nullable=False))
        batch_op.add_column(sa.Column('external_author', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('external_message_id', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint(
            'uq_board_chat_message_source_external', ['source', 'external_message_id'])
    with op.batch_alter_table('telegram_settings') as batch_op:
        batch_op.add_column(sa.Column('board_chat_id', sa.String(length=64), nullable=True))
    with op.batch_alter_table('max_settings') as batch_op:
        batch_op.add_column(sa.Column('board_chat_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('max_settings') as batch_op:
        batch_op.drop_column('board_chat_id')
    with op.batch_alter_table('telegram_settings') as batch_op:
        batch_op.drop_column('board_chat_id')
    op.execute("DELETE FROM board_chat_message WHERE author_id IS NULL")
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.drop_constraint('uq_board_chat_message_source_external', type_='unique')
        batch_op.drop_column('external_message_id')
        batch_op.drop_column('external_author')
        batch_op.drop_column('source')
        batch_op.alter_column('author_id', existing_type=sa.Integer(), nullable=False)
