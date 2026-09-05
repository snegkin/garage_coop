"""add must_change_password to user

Revision ID: 19bf37b971fd
Revises: 96561af74114
Create Date: 2026-09-05 21:06:20.973786

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '19bf37b971fd'
down_revision: Union[str, Sequence[str], None] = '96561af74114'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    server_default=false нужен только на время добавления колонки — все
    существующие учётные записи никогда не были принудительно созданы этой
    фичей, менять пароль при входе им не нужно. Дальше server_default
    убирается: на уровне приложения значение всегда задаётся явно (тот же
    приём, что и в 57671066ae31_add_document_type_extension_and_).
    """
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('must_change_password', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('must_change_password')
