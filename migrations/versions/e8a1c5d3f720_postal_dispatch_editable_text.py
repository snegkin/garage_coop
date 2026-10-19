"""postal dispatch editable text

Свой текст вводного/заключительного абзацев заказного письма и виды
задолженности, на которые оно формировалось (см. app/models.py:
PostalDispatch, app/postal_letters.py: edit), и счётчик номеров писем по
годам (PostalLetterCounter, номер вида «2026-1»).

Revision ID: e8a1c5d3f720
Revises: d9f4b6c8e217
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8a1c5d3f720'
down_revision: Union[str, Sequence[str], None] = 'd9f4b6c8e217'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('postal_dispatch', schema=None) as batch_op:
        batch_op.add_column(sa.Column('intro_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('closing_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('categories', sa.Text(), nullable=True))
    op.create_table(
        'postal_letter_counter',
        sa.Column('year', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('last_number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('year'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('postal_letter_counter')
    with op.batch_alter_table('postal_dispatch', schema=None) as batch_op:
        batch_op.drop_column('categories')
        batch_op.drop_column('closing_text')
        batch_op.drop_column('intro_text')
