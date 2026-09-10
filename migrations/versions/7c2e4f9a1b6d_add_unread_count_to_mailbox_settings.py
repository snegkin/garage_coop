"""add unread_count to mailbox settings

Кэш числа непрочитанных писем для бейджа в шапке сайта — считается
cron-скриптом (scripts/poll_mailbox.py), не на каждый запрос, см.
app/models.py: MailboxSettings.unread_count.

Revision ID: 7c2e4f9a1b6d
Revises: 1f4b9c0d7a3e
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c2e4f9a1b6d'
down_revision: Union[str, Sequence[str], None] = '1f4b9c0d7a3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    server_default='0' нужен только на время добавления колонки — иначе
    batch-пересборка таблицы (SQLite: копирование в новую, см.
    batch_alter_table) не может скопировать существующую строку настроек
    без значения в NOT NULL колонке. Дальше server_default убирается
    ОТДЕЛЬНЫМ batch-блоком (тот же приём, что и в
    602455be5ca3_add_notification_preferences) — если сделать это в ОДНОМ
    блоке с add_column, alembic соберёт схему новой таблицы уже без
    default ДО копирования данных, и копирование упадёт с той же ошибкой.

    Колонка добавляется только если её ещё нет — SQLite коммитит DDL
    немедленно (ADD COLUMN — не часть отменяемой транзакции), поэтому если
    предыдущий прогон этой миграции упал НА СЛЕДУЮЩЕМ шаге (например, при
    накатывании одной из следующих миграций в этом же вызове `alembic
    upgrade`), alembic_version не продвинется, и при повторном запуске эта
    функция выполнится заново — без проверки словили бы "duplicate column
    name" на уже добавленной колонке.
    """
    inspector = sa.inspect(op.get_bind())
    if "unread_count" not in {col["name"] for col in inspector.get_columns("mailbox_settings")}:
        with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
            batch_op.add_column(sa.Column('unread_count', sa.Integer(), nullable=False, server_default='0'))
        with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
            batch_op.alter_column('unread_count', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.drop_column('unread_count')
