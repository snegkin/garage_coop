"""add notification preferences

Подписка на уведомления о событиях сайта (начисление/платёж на свой счёт,
новость/объявление, форум, чат правления) — настройки в профиле, см.
app/notifications.py, app/cabinet.py: profile. Один канал доставки на
человека (notify_channel) плюс отдельные подписки-флаги на каждое событие.

Revision ID: 602455be5ca3
Revises: 35e0b519fb7e
Create Date: 2026-09-09 20:27:01.276925

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '602455be5ca3'
down_revision: Union[str, Sequence[str], None] = '35e0b519fb7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    server_default=false для булевых колонок нужен только на время
    добавления — ни у кого ещё не может быть включена подписка на событие,
    которого до этой ревизии не существовало. Дальше server_default
    убирается (тот же приём, что и в 8ea4ad8474d7_add_board_chat_presence_fields).
    """
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_channel', sa.Enum('EMAIL', 'TELEGRAM', 'VK', 'MAX', name='notificationchannel'), nullable=True))
        batch_op.add_column(sa.Column('notify_charge', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('notify_payment', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('notify_news', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('notify_forum', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('notify_board_chat', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('board_chat_notified_message_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('notify_charge', server_default=None)
        batch_op.alter_column('notify_payment', server_default=None)
        batch_op.alter_column('notify_news', server_default=None)
        batch_op.alter_column('notify_forum', server_default=None)
        batch_op.alter_column('notify_board_chat', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('board_chat_notified_message_id')
        batch_op.drop_column('notify_board_chat')
        batch_op.drop_column('notify_forum')
        batch_op.drop_column('notify_news')
        batch_op.drop_column('notify_payment')
        batch_op.drop_column('notify_charge')
        batch_op.drop_column('notify_channel')
