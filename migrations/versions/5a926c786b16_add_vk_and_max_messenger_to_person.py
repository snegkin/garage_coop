"""add vk and max messenger to person

Revision ID: 5a926c786b16
Revises: f8c06f984fce
Create Date: 2026-09-04 20:09:27.070855

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a926c786b16'
down_revision: Union[str, Sequence[str], None] = 'f8c06f984fce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 'cooperative.garage_area' исключён из автогенерации — это несвязанный
    # с этой миграцией дрейф схемы (колонка уже отсутствует в models.py, но
    # осталась в БД с предыдущей миграции), трогать его в рамках задачи
    # "добавить vk/max в карточку члена" не нужно.
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vk', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('max_messenger', sa.String(length=120), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('person', schema=None) as batch_op:
        batch_op.drop_column('max_messenger')
        batch_op.drop_column('vk')
