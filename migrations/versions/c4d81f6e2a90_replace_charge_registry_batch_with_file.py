"""replace charge registry batch with file

charge_registry_batch — история отправок реестра начислений через API —
удаляется: отправки не было и не будет, API Сбербанка принимает реестр
начислений (/v1/debt-registries) только в интеграции для холдингов (см.
app/bank_api/sberbank.py). Таблица ни разу не заполнялась.

Взамен charge_registry_file — сформированные файлы реестра начислений для
ручной загрузки в СберБизнес Онлайн, с самим содержимым файла (см.
models.ChargeRegistryFile).

Revision ID: c4d81f6e2a90
Revises: a7c3e9d2b415
Create Date: 2026-09-29 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d81f6e2a90'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9d2b415'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('charge_registry_batch', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_charge_registry_batch_bank_account_id'))

    op.drop_table('charge_registry_batch')

    op.create_table('charge_registry_file',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bank_account_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('filename', sa.String(length=120), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('rows_count', sa.Integer(), nullable=False),
    sa.Column('debtors_count', sa.Integer(), nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.ForeignKeyConstraint(['bank_account_id'], ['bank_account.id'], name=op.f('fk_charge_registry_file_bank_account_id_bank_account'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name=op.f('fk_charge_registry_file_created_by_user_id_user'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_charge_registry_file'))
    )
    with op.batch_alter_table('charge_registry_file', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_charge_registry_file_bank_account_id'), ['bank_account_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('charge_registry_file', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_charge_registry_file_bank_account_id'))

    op.drop_table('charge_registry_file')

    op.create_table('charge_registry_batch',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bank_account_id', sa.Integer(), nullable=False),
    sa.Column('period', sa.String(length=20), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'ERROR', name='chargeregistrystatus'), nullable=False),
    sa.Column('charges_count', sa.Integer(), nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('bank_comment', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['bank_account_id'], ['bank_account.id'], name=op.f('fk_charge_registry_batch_bank_account_id_bank_account'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_charge_registry_batch'))
    )
    with op.batch_alter_table('charge_registry_batch', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_charge_registry_batch_bank_account_id'), ['bank_account_id'], unique=False)
