"""add is_inline to wiki attachment

Revision ID: ac070ea03392
Revises: 2a6e40f47ad9
Create Date: 2026-09-05 08:47:50.659325

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ac070ea03392'
down_revision: Union[str, Sequence[str], None] = '2a6e40f47ad9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    'cooperative.garage_area' исключён из автогенерации — несвязанный с
    этой миграцией дрейф схемы (колонки уже нет в models.py, но она
    осталась в БД с более ранней миграции), не в рамках этой задачи.

    server_default=true нужен только на время добавления колонки — ВСЕ
    существующие на момент этой миграции строки wiki_attachment по смыслу
    inline (раньше отдельного is_inline не было вовсе, единственное
    назначение вложения — быть встроенным в текст, см. models.py). Дальше
    server_default убирается: на уровне приложения значение всегда
    задаётся явно (тот же приём, что и в 57671066ae31_add_document_type_extension).
    """
    with op.batch_alter_table('wiki_attachment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_inline', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.create_index(batch_op.f('ix_wiki_attachment_is_inline'), ['is_inline'], unique=False)
    with op.batch_alter_table('wiki_attachment', schema=None) as batch_op:
        batch_op.alter_column('is_inline', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('wiki_attachment', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_attachment_is_inline'))
        batch_op.drop_column('is_inline')
