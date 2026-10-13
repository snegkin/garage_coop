"""add failure notification

Отметка последнего уведомления правлению о сбое фоновой синхронизации —
не чаще раза в сутки на источник (см. app/notifications.py: notify_failure).

Revision ID: f2b8d4c6a913
Revises: e1a7c3b9d402
Create Date: 2026-10-13 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b8d4c6a913'
down_revision: Union[str, Sequence[str], None] = 'e1a7c3b9d402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'failure_notification',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=100), nullable=False),
        sa.Column('notified_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('failure_notification')
