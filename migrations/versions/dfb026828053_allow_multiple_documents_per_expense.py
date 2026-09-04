"""allow multiple documents per expense

Revision ID: dfb026828053
Revises: 5a926c786b16
Create Date: 2026-09-04 23:10:55.076877

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dfb026828053'
down_revision: Union[str, Sequence[str], None] = '5a926c786b16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    'cooperative.garage_area' исключён из автогенерации — несвязанный с
    этой миграцией дрейф схемы (колонки уже нет в models.py, но она
    осталась в БД с более ранней миграции), не в рамках этой задачи.
    """
    with op.batch_alter_table('document', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expense_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_document_expense_id'), ['expense_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_document_expense_id_expense'), 'expense', ['expense_id'], ['id'], ondelete='SET NULL')

    # Переносим существующие ссылки expense.document_id -> document.expense_id
    # ДО удаления старой колонки, чтобы не потерять уже сохранённые привязки.
    op.execute(
        "UPDATE document SET expense_id = ("
        "  SELECT e.id FROM expense e WHERE e.document_id = document.id"
        ") WHERE EXISTS (SELECT 1 FROM expense e WHERE e.document_id = document.id)"
    )

    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expense_document_id'))
        batch_op.drop_constraint(batch_op.f('fk_expense_document_id_document'), type_='foreignkey')
        batch_op.drop_column('document_id')


def downgrade() -> None:
    """Downgrade schema.

    Лоссово для расхода с НЕСКОЛЬКИМИ документами (новая возможность этой
    миграции) — старая схема допускала только один document_id на расход,
    так что при откате сохраняется произвольный один из них (первый по id).
    """
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.add_column(sa.Column('document_id', sa.INTEGER(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_expense_document_id_document'), 'document', ['document_id'], ['id'], ondelete='SET NULL')
        batch_op.create_index(batch_op.f('ix_expense_document_id'), ['document_id'], unique=False)

    op.execute(
        "UPDATE expense SET document_id = ("
        "  SELECT d.id FROM document d WHERE d.expense_id = expense.id ORDER BY d.id LIMIT 1"
        ") WHERE EXISTS (SELECT 1 FROM document d WHERE d.expense_id = expense.id)"
    )

    with op.batch_alter_table('document', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_document_expense_id_expense'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_document_expense_id'))
        batch_op.drop_column('expense_id')
