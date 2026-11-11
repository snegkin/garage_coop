"""Модульбанк вместо ВТБ и Т-Банка: провайдер API и токен Модульбанка

Revision ID: d5e8a2c71f03
Revises: b7e2c19d4f60
Create Date: 2026-11-11 12:00:00

От ВТБ кооператив отказался в пользу Модульбанка, Т-Банком не пользовался.
VTB и TBANK в перечне были только заготовками (клиентов не было) — счета,
где их успели выбрать, переводятся в NONE. Добавляется
api_token_encrypted — единственный реквизит подключения к Модульбанку.
Enum-колонки в проекте хранят ИМЯ члена перечисления (NONE/SBERBANK/…).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e8a2c71f03'
down_revision: Union[str, Sequence[str], None] = 'b7e2c19d4f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_ENUM = sa.Enum('NONE', 'SBERBANK', 'VTB', 'TBANK', name='bankapiprovider')
NEW_ENUM = sa.Enum('NONE', 'SBERBANK', 'MODULBANK', name='bankapiprovider')


def upgrade() -> None:
    op.execute("UPDATE bank_account SET api_provider = 'NONE' WHERE api_provider IN ('VTB', 'TBANK')")
    with op.batch_alter_table('bank_account', schema=None) as batch_op:
        batch_op.alter_column('api_provider', existing_type=OLD_ENUM, type_=NEW_ENUM, existing_nullable=False)
    with op.batch_alter_table('bank_api_credential', schema=None) as batch_op:
        batch_op.add_column(sa.Column('api_token_encrypted', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('bank_api_credential', schema=None) as batch_op:
        batch_op.drop_column('api_token_encrypted')
    op.execute("UPDATE bank_account SET api_provider = 'NONE' WHERE api_provider = 'MODULBANK'")
    with op.batch_alter_table('bank_account', schema=None) as batch_op:
        batch_op.alter_column('api_provider', existing_type=NEW_ENUM, type_=OLD_ENUM, existing_nullable=False)
