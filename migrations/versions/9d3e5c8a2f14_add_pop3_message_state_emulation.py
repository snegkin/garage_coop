"""add pop3 message state emulation

POP3 не хранит флаги "прочитано"/"важное" на сервере — mailbox_pop3_message_state
эмулирует их персонально для каждого члена правления (message_uidl — RFC 1939
UIDL, устойчивый идентификатор письма). user.pop3_mailbox_unread_count —
кэш числа непрочитанных для бейджа в шапке (см. app/models.py:
MailboxPop3MessageState, User.pop3_mailbox_unread_count).

Revision ID: 9d3e5c8a2f14
Revises: 7c2e4f9a1b6d
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d3e5c8a2f14'
down_revision: Union[str, Sequence[str], None] = '7c2e4f9a1b6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Обе части (новая таблица, новая колонка user) добавляются только если
    их ещё нет — SQLite коммитит DDL немедленно, поэтому если предыдущий
    прогон упал НА ОДНОЙ из двух частей (например, таблица создалась, а
    колонка user — нет, или наоборот), alembic_version не продвинется, и
    при повторном запуске эта функция выполнится заново целиком; без
    проверки словили бы "table already exists"/"duplicate column name"
    на уже применённой части (см. то же самое в
    7c2e4f9a1b6d_add_unread_count_to_mailbox_settings)."""
    inspector = sa.inspect(op.get_bind())

    if "mailbox_pop3_message_state" not in inspector.get_table_names():
        op.create_table('mailbox_pop3_message_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('message_uidl', sa.String(length=255), nullable=False),
        sa.Column('seen', sa.Boolean(), nullable=False),
        sa.Column('flagged', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_mailbox_pop3_message_state_user_id_user'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_mailbox_pop3_message_state')),
        sa.UniqueConstraint('user_id', 'message_uidl', name='uq_mailbox_pop3_state_user_message'),
        )
        with op.batch_alter_table('mailbox_pop3_message_state', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_mailbox_pop3_message_state_user_id'), ['user_id'], unique=False)

    # server_default='0' временно, до конца этого блока — см. подробное
    # объяснение в 7c2e4f9a1b6d_add_unread_count_to_mailbox_settings (та
    # же ошибка на непустой таблице user, если убрать default в ОДНОМ
    # batch-блоке с add_column).
    if "pop3_mailbox_unread_count" not in {col["name"] for col in inspector.get_columns("user")}:
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.add_column(sa.Column('pop3_mailbox_unread_count', sa.Integer(), nullable=False, server_default='0'))
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.alter_column('pop3_mailbox_unread_count', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('pop3_mailbox_unread_count')

    with op.batch_alter_table('mailbox_pop3_message_state', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mailbox_pop3_message_state_user_id'))

    op.drop_table('mailbox_pop3_message_state')
