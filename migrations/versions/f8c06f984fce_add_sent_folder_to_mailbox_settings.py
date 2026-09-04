"""add sent folder to mailbox settings

Папка "Отправленные" (IMAP) — SMTP сам не сохраняет копию письма, после
отправки APPEND делается в эту папку (см. mail_client.send_message).
Имя папки председатель указывает сам — без привязки к провайдеру.

Примечание: autogenerate заодно предложил удалить cooperative.garage_area
(колонка должна была уйти ещё в remove_garage_area_column, но осталась в
БД этого окружения) — это НЕ трогаем в этой миграции, чтобы не смешивать
несвязанные изменения схемы; см. отдельное обсуждение с пользователем.

Revision ID: f8c06f984fce
Revises: d3b61bbcf71d
Create Date: 2026-09-04 12:02:10.366945

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8c06f984fce'
down_revision: Union[str, Sequence[str], None] = 'd3b61bbcf71d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sent_folder', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.drop_column('sent_folder')
