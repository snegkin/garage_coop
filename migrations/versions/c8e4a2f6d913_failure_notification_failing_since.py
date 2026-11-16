"""failure notification failing_since

Начало текущей серии неудач — уведомление о сбое регистратора только если
кадры не снимаются непрерывно не меньше 10 минут, см. app/notifications.py:
notify_failure (min_duration).

Revision ID: c8e4a2f6d913
Revises: b7c2e9f4a1d6
Create Date: 2026-11-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e4a2f6d913'
down_revision: Union[str, Sequence[str], None] = 'b7c2e9f4a1d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('failure_notification', schema=None) as batch_op:
        batch_op.add_column(sa.Column('failing_since', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('failure_notification', schema=None) as batch_op:
        batch_op.drop_column('failing_since')
