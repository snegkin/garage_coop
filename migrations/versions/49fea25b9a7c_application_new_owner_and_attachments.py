"""application new owner and attachments

Сведения о новом собственнике в заявлении о смене собственника, кто им
стал после проведения смены правлением, и файлы-приложения к заявлению
(см. app/models.py: Application, ApplicationAttachment).

Revision ID: 49fea25b9a7c
Revises: 6220448af17f
Create Date: 2026-10-20 18:27:23.325892

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '49fea25b9a7c'
down_revision: Union[str, Sequence[str], None] = '6220448af17f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('application_attachment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('file_path', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['application.id'], name=op.f('fk_application_attachment_application_id_application'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_application_attachment'))
    )
    with op.batch_alter_table('application_attachment', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_application_attachment_application_id'), ['application_id'], unique=False)

    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.add_column(sa.Column('new_owner_data', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('new_owner_person_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('transferred_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_application_new_owner_person_id_person'), 'person', ['new_owner_person_id'], ['id'], ondelete='SET NULL')



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_application_new_owner_person_id_person'), type_='foreignkey')
        batch_op.drop_column('transferred_at')
        batch_op.drop_column('new_owner_person_id')
        batch_op.drop_column('new_owner_data')

    with op.batch_alter_table('application_attachment', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_application_attachment_application_id'))

    op.drop_table('application_attachment')
