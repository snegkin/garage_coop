"""failure notification consecutive count

Счёт неудачных прогонов подряд — уведомление о сбое частых cron-задач
(eWeLink, регистратор) только после нескольких неудач подряд, см.
app/notifications.py: notify_failure. notified_at теперь пустой, пока
порог не набран.

Revision ID: a3c9e5f7b214
Revises: f2b8d4c6a913
Create Date: 2026-10-13 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c9e5f7b214'
down_revision: Union[str, Sequence[str], None] = 'f2b8d4c6a913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('failure_notification', schema=None) as batch_op:
        batch_op.add_column(sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'))
        batch_op.alter_column('notified_at', existing_type=sa.DateTime(), nullable=True)
    with op.batch_alter_table('failure_notification', schema=None) as batch_op:
        batch_op.alter_column('consecutive_failures', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM failure_notification WHERE notified_at IS NULL")
    with op.batch_alter_table('failure_notification', schema=None) as batch_op:
        batch_op.alter_column('notified_at', existing_type=sa.DateTime(), nullable=False)
        batch_op.drop_column('consecutive_failures')
