"""postal api agent

Отправка заказных писем через API ИС ЭПС агентом ЭЗП (см. app/models.py:
PostalDispatch, PostalApiSettings, app/postal_letters.py, ezp_agent/):
поля очереди/requestCode/статуса Почты у письма и настройки подключения.
Новое значение статуса QUEUED колонку не меняет — статус хранится строкой
VARCHAR(9) без CHECK.

Revision ID: f2c7d4a9b318
Revises: e5b81c3d9a42
Create Date: 2026-10-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c7d4a9b318'
down_revision: Union[str, Sequence[str], None] = 'e5b81c3d9a42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('postal_dispatch', schema=None) as batch_op:
        batch_op.add_column(sa.Column('queued_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('api_request_code', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('api_status', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('api_checked_at', sa.DateTime(), nullable=True))
    op.create_table(
        'postal_api_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sender_id', sa.String(length=64), nullable=True),
        sa.Column('agent_token_hash', sa.String(length=64), nullable=True),
        sa.Column('agent_last_seen_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_postal_api_settings')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('postal_api_settings')
    with op.batch_alter_table('postal_dispatch', schema=None) as batch_op:
        batch_op.drop_column('api_checked_at')
        batch_op.drop_column('api_status')
        batch_op.drop_column('api_request_code')
        batch_op.drop_column('queued_at')
