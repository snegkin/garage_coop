"""add drafts and spam folders to mailbox settings

"Черновики"/"Спам" (IMAP) — папки только для просмотра (в отличие от
sent_folder/trash_folder приложение само в них ничего не пишет и не
перемещает), см. app/models.py: MailboxSettings.

Revision ID: 1f4b9c0d7a3e
Revises: 086f732f53c7
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f4b9c0d7a3e'
down_revision: Union[str, Sequence[str], None] = '086f732f53c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('drafts_folder', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('spam_folder', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.drop_column('spam_folder')
        batch_op.drop_column('drafts_folder')
