"""add board and member event notifications

Уведомления правлению о событиях журнала аудита по группам
(notify_board_events, см. app/notifications.py: BOARD_EVENT_GROUPS) и
новые подписки членов: открыто голосование, собрание, решение по своей
заявке, опубликован документ/страница вики.

Revision ID: e1a7c3b9d402
Revises: d5f9b2a3e7c1
Create Date: 2026-10-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1a7c3b9d402'
down_revision: Union[str, Sequence[str], None] = 'd5f9b2a3e7c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    В отличие от 602455be5ca3_add_notification_preferences, новые подписки
    членов включаются и уже существующим учётным записям (server_default=true
    на время добавления) — так же, как они включены по умолчанию новым.
    notify_board_events остаётся NULL — значит «набор по умолчанию».
    """
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_vote', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('notify_meeting', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('notify_my_requests', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('notify_documents', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('notify_board_events', sa.Text(), nullable=True))
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('notify_vote', server_default=None)
        batch_op.alter_column('notify_meeting', server_default=None)
        batch_op.alter_column('notify_my_requests', server_default=None)
        batch_op.alter_column('notify_documents', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('notify_board_events')
        batch_op.drop_column('notify_documents')
        batch_op.drop_column('notify_my_requests')
        batch_op.drop_column('notify_meeting')
        batch_op.drop_column('notify_vote')
