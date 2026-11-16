"""garage contact is_tenant

GarageContact.is_tenant — лицо для связи снимает гараж (арендатор).

Revision ID: e2a8b4d6f193
Revises: d1f7a3c9e528
Create Date: 2026-11-16 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2a8b4d6f193'
down_revision: Union[str, Sequence[str], None] = 'd1f7a3c9e528'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('garage_contact') as batch_op:
        batch_op.add_column(sa.Column('is_tenant', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('garage_contact') as batch_op:
        batch_op.drop_column('is_tenant')
