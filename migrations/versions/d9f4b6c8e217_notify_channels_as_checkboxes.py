"""notify channels as checkboxes

Один способ доставки уведомлений (user.notify_channel) → несколько
независимых галочек: notify_via_email/telegram/webpush (см. app/models.py:
User, app/notifications.py). notify_via_webpush трёхзначный: NULL — ещё
не выбирал (галочка «Разрешить уведомления» на форме входа его включит).

Перенос: выбранный раньше канал → его галочка. Пустой notify_channel был у
учётных записей, созданных до появления настроек и ни разу их не
сохранявших, — им ничего не включаем, push остаётся «не выбран».

Revision ID: d9f4b6c8e217
Revises: c7e2a4b6d935
Create Date: 2026-10-14 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f4b6c8e217'
down_revision: Union[str, Sequence[str], None] = 'c7e2a4b6d935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_via_email', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('notify_via_telegram', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('notify_via_webpush', sa.Boolean(), nullable=True))
    op.execute("""
        UPDATE "user" SET
            notify_via_email = CASE WHEN notify_channel = 'EMAIL' THEN 1 ELSE 0 END,
            notify_via_telegram = CASE WHEN notify_channel = 'TELEGRAM' THEN 1 ELSE 0 END,
            notify_via_webpush = CASE
                WHEN notify_channel = 'WEBPUSH' THEN 1
                WHEN notify_channel IS NULL THEN NULL
                ELSE 0
            END
    """)
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('notify_via_email', server_default=None)
        batch_op.alter_column('notify_via_telegram', server_default=None)
        batch_op.drop_column('notify_channel')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'notify_channel', sa.Enum('EMAIL', 'TELEGRAM', 'WEBPUSH', name='notificationchannel'), nullable=True,
        ))
    op.execute("""
        UPDATE "user" SET notify_channel = CASE
            WHEN notify_via_webpush = 1 THEN 'WEBPUSH'
            WHEN notify_via_telegram = 1 THEN 'TELEGRAM'
            WHEN notify_via_email = 1 THEN 'EMAIL'
            ELSE NULL
        END
    """)
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('notify_via_webpush')
        batch_op.drop_column('notify_via_telegram')
        batch_op.drop_column('notify_via_email')
