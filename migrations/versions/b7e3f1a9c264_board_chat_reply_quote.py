"""board chat reply quote

BoardChatMessage.reply_quote — выделенный фрагмент сообщения, на который
отвечают (частичная цитата, как в Telegram). См. app/board_chat.py.

Revision ID: b7e3f1a9c264
Revises: a4d8e2f6c913
Create Date: 2026-11-17 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e3f1a9c264'
down_revision: Union[str, Sequence[str], None] = 'a4d8e2f6c913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.add_column(sa.Column('reply_quote', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('board_chat_message') as batch_op:
        batch_op.drop_column('reply_quote')
