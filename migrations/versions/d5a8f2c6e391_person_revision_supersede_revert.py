"""Ревизии карточек: «заменён более поздним» и отмена одобрения

Revision ID: d5a8f2c6e391
Revises: b9e3d7a1c428
Create Date: 2026-11-18 18:00:00

Новые статусы SUPERSEDED (пришёл более поздний запрос того же человека) и
REVERTED (председатель отменил одобрение), снимок карточки перед одобрением
(applied_over_snapshot) и кто/когда отменил одобрение.

Уже ожидающие запросы, у которых есть более поздний запрос того же
человека, помечаются SUPERSEDED — одобрить их по ошибке больше нельзя.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a8f2c6e391'
down_revision: Union[str, Sequence[str], None] = 'b9e3d7a1c428'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUS = sa.Enum('PENDING', 'APPROVED', 'REJECTED', name='persondatarevisionstatus')
NEW_STATUS = sa.Enum('PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'REVERTED', name='persondatarevisionstatus')


def upgrade() -> None:
    with op.batch_alter_table('person_data_revision', schema=None) as batch_op:
        batch_op.alter_column('status', existing_type=OLD_STATUS, type_=NEW_STATUS, existing_nullable=False)
        batch_op.add_column(sa.Column('applied_over_snapshot', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('reverted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('reverted_by_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_person_data_revision_reverted_by_user_id_user'), 'user', ['reverted_by_user_id'], ['id'],
        )

    op.get_bind().execute(sa.text("""
        UPDATE person_data_revision
        SET status = 'SUPERSEDED'
        WHERE status = 'PENDING'
          AND EXISTS (
            SELECT 1 FROM person_data_revision AS later
            WHERE later.person_id = person_data_revision.person_id
              AND later.submitted_at > person_data_revision.submitted_at
          )
    """))


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE person_data_revision SET status = 'REJECTED' WHERE status IN ('SUPERSEDED', 'REVERTED')"))
    with op.batch_alter_table('person_data_revision', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_person_data_revision_reverted_by_user_id_user'), type_='foreignkey')
        batch_op.drop_column('reverted_by_user_id')
        batch_op.drop_column('reverted_at')
        batch_op.drop_column('applied_over_snapshot')
        batch_op.alter_column('status', existing_type=NEW_STATUS, type_=OLD_STATUS, existing_nullable=False)
