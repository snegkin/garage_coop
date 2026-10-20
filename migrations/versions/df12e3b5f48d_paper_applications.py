"""paper applications

Бумажные заявления, заведённые правлением по скану подписанного бланка
(см. app/models.py: Application.registered_by_user_id,
ApplicationAttachment.is_scan).

Revision ID: df12e3b5f48d
Revises: 49fea25b9a7c
Create Date: 2026-10-20 18:42:26.723802

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'df12e3b5f48d'
down_revision: Union[str, Sequence[str], None] = '49fea25b9a7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.add_column(sa.Column('registered_by_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_application_registered_by_user_id_user'), 'user', ['registered_by_user_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('application_attachment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_scan', sa.Boolean(), server_default=sa.text('0'), nullable=False))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('application_attachment', schema=None) as batch_op:
        batch_op.drop_column('is_scan')

    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_application_registered_by_user_id_user'), type_='foreignkey')
        batch_op.drop_column('registered_by_user_id')

