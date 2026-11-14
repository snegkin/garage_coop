"""Факсимиле: изображения печати кооператива и подписи председателя

Revision ID: b6e2f40a9d13
Revises: a3d7e91c5b20
Create Date: 2026-11-13 22:30:00

cooperative.seal_png, person.signature_png — PNG с прозрачным фоном
(app/facsimile.py), вставляются в печатные формы только галочкой.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6e2f40a9d13'
down_revision: Union[str, Sequence[str], None] = 'a3d7e91c5b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cooperative') as batch_op:
        batch_op.add_column(sa.Column('seal_png', sa.LargeBinary(), nullable=True))
    with op.batch_alter_table('person') as batch_op:
        batch_op.add_column(sa.Column('signature_png', sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('person') as batch_op:
        batch_op.drop_column('signature_png')
    with op.batch_alter_table('cooperative') as batch_op:
        batch_op.drop_column('seal_png')
