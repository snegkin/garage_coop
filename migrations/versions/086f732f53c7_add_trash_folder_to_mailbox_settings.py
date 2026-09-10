"""add trash folder to mailbox settings

Папка "Корзина" (IMAP) — при удалении письма (см. mailbox.delete_message)
оно перемещается сюда (COPY+expunge), а не удаляется безвозвратно, если
председатель указал имя папки. NULL по умолчанию (в отличие от sent_folder)
— старое поведение (безвозвратное удаление) для уже настроенных ящиков не
меняется молча.

Revision ID: 086f732f53c7
Revises: 62f3a0a3bdba
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '086f732f53c7'
down_revision: Union[str, Sequence[str], None] = '62f3a0a3bdba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trash_folder', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.drop_column('trash_folder')
