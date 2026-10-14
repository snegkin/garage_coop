"""rename pd_consent_at to pd_notice_at

Обработка персональных данных — на основании устава (п. 5 ч. 1 ст. 6
152-ФЗ), согласие для неё не требуется; поле фиксирует лишь, когда
человеку при входе впервые показаны условия обработки.

Revision ID: c7e2a4b6d935
Revises: b5d1f3a8c720
Create Date: 2026-10-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7e2a4b6d935'
down_revision: Union[str, Sequence[str], None] = 'b5d1f3a8c720'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('pd_consent_at', new_column_name='pd_notice_at')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('pd_notice_at', new_column_name='pd_consent_at')
