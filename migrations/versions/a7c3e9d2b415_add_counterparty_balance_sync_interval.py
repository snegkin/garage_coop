"""add counterparty balance sync interval

counterparty.balance_sync_interval_days — как часто cron обновляет баланс
личного кабинета контрагента (1/2/3/7/30 дней, см.
models.BALANCE_SYNC_INTERVAL_CHOICES). По умолчанию 1 — прежнее поведение
(ежедневно).

Revision ID: a7c3e9d2b415
Revises: f1151ccf7863
Create Date: 2026-09-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d2b415'
down_revision: Union[str, Sequence[str], None] = 'f1151ccf7863'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.add_column(sa.Column('balance_sync_interval_days', sa.Integer(), server_default='1', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('counterparty', schema=None) as batch_op:
        batch_op.drop_column('balance_sync_interval_days')
