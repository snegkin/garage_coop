"""unify counterparty api credentials, sms settings points to counterparty

Переносит реквизиты SMS Aero (email/api_key/подпись отправителя) из
отдельного singleton SmsSettings на карточку контрагента
(CounterpartyApiCredential, generic login/secret_encrypted/extra — общая
таблица под ВСЕ провайдеры с собственными кредами, не только SMS Aero, см.
docstring CounterpartyApiCredential в app/models.py) — по прямой просьбе,
чтобы у всех контрагентов с API была одна и та же точка настройки на их же
карточке, а не разные (SMS Aero — в /sms/, остальные — на карточке).
SmsSettings после этого хранит не сами креды, а ссылку (counterparty_id),
какой контрагент их сейчас предоставляет — председатель выбирает его явно
на /sms/, а не полагается на «в системе есть только один SMSAERO».

Бэкофилл (тот же приём, что e80abefa5b98 — сырой SQL через op.get_bind(),
без импорта живых моделей): если в sms_settings была настроенная запись
(непустые smsaero_email И smsaero_api_key_encrypted), создаём для неё новый
Counterparty ("SMS Aero", api_provider='SMSAERO') + CounterpartyApiCredential
(login=email, secret_encrypted=<копия как есть — тот же Fernet-ключ, не
расшифровываем/шифруем заново>, extra=sender_sign) и проставляем
sms_settings.counterparty_id. Если записи нет или она не настроена (свежий
деплой) — бэкофилл не создаёт ничего лишнего.

Revision ID: d1786b15be12
Revises: c9ecc27410dc
Create Date: 2026-09-14 11:01:09.341022

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1786b15be12'
down_revision: Union[str, Sequence[str], None] = 'c9ecc27410dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    op.create_table('counterparty_api_credential',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('counterparty_id', sa.Integer(), nullable=False),
    sa.Column('login', sa.String(length=255), nullable=True),
    sa.Column('secret_encrypted', sa.Text(), nullable=True),
    sa.Column('extra', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['counterparty_id'], ['counterparty.id'], name=op.f('fk_counterparty_api_credential_counterparty_id_counterparty'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_counterparty_api_credential')),
    sa.UniqueConstraint('counterparty_id', name=op.f('uq_counterparty_api_credential_counterparty_id'))
    )

    # Колонку-ссылку добавляем ОТДЕЛЬНО от дропа старых колонок ниже — нужно
    # успеть прочитать их значения для бэкофилла, прежде чем batch-режим
    # пересоздаст таблицу без них.
    with op.batch_alter_table('sms_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('counterparty_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_sms_settings_counterparty_id_counterparty'), 'counterparty', ['counterparty_id'], ['id'], ondelete='SET NULL')

    row = conn.execute(sa.text(
        "SELECT id, smsaero_email, smsaero_api_key_encrypted, sender_sign FROM sms_settings LIMIT 1"
    )).fetchone()
    created = False
    if row is not None and row[1] and row[2]:
        settings_id, email, api_key_encrypted, sender_sign = row
        result = conn.execute(sa.text(
            "INSERT INTO counterparty (name, api_provider) VALUES ('SMS Aero', 'SMSAERO')"
        ))
        counterparty_id = result.lastrowid
        conn.execute(sa.text(
            "INSERT INTO counterparty_api_credential (counterparty_id, login, secret_encrypted, extra) "
            "VALUES (:cid, :login, :secret, :extra)"
        ), {"cid": counterparty_id, "login": email, "secret": api_key_encrypted, "extra": sender_sign})
        conn.execute(sa.text(
            "UPDATE sms_settings SET counterparty_id = :cid WHERE id = :sid"
        ), {"cid": counterparty_id, "sid": settings_id})
        created = True

    print(f"Реквизиты SMS Aero перенесены на карточку контрагента: {'да' if created else 'нечего переносить'}")

    with op.batch_alter_table('sms_settings', schema=None) as batch_op:
        batch_op.drop_column('smsaero_email')
        batch_op.drop_column('smsaero_api_key_encrypted')
        batch_op.drop_column('sender_sign')
        batch_op.drop_column('provider')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sms_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provider', sa.VARCHAR(length=7), nullable=False, server_default='SMSAERO'))
        batch_op.add_column(sa.Column('sender_sign', sa.VARCHAR(length=50), nullable=True))
        batch_op.add_column(sa.Column('smsaero_api_key_encrypted', sa.TEXT(), nullable=True))
        batch_op.add_column(sa.Column('smsaero_email', sa.VARCHAR(length=255), nullable=True))
    with op.batch_alter_table('sms_settings', schema=None) as batch_op:
        batch_op.alter_column('provider', server_default=None)
        batch_op.drop_constraint(batch_op.f('fk_sms_settings_counterparty_id_counterparty'), type_='foreignkey')
        batch_op.drop_column('counterparty_id')

    op.drop_table('counterparty_api_credential')
    # Примечание: downgrade НЕ переносит креды обратно из
    # counterparty_api_credential в sms_settings — при нескольких
    # контрагентах с API неоднозначно, какой из них «тот самый» (тот же
    # осознанный компромисс, что в e80abefa5b98). Если нужно откатиться —
    # реквизиты SMS Aero придётся ввести на /sms/ заново.
