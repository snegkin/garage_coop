"""add telecom disputes fee type and personal member accounts

Новый вид взноса "Телекоммуникационные услуги и споры" (code
telecom_disputes) — по прямой просьбе, для взыскания с конкретного члена
кооператива двух видов расходов, которые кооператив несёт из-за него лично,
а не из-за владения гаражом: платного SMS с кодом при восстановлении
пароля (см. app/auth.py:forgot_password, app/sms/, предупреждение на
странице восстановления пароля) и госпошлины, уплаченной кооперативом по
иску к должнику (см. app/legal_docs.py — начисляется вручную кнопкой после
факта оплаты). Оба расхода не привязаны ни к какому конкретному гаражу
человека (если их несколько) — поэтому FeeType обзавёлся новым флагом
per_garage=False (см. models.FeeType), а MemberAccount.garage_id стал
nullable: счёт этого вида — один на человека сразу за все его гаражи,
заводится не через обычный garages._ensure_member_accounts (тот — per
гараж), а через accounting.ensure_personal_member_accounts.

SmsLog обзавёлся полями purpose/person_id/provider_message_id/charge_id —
только для восстановления пароля (единственное место, которое их
заполняет, см. auth.forgot_password) отслеживать, какая SMS уже
начислена, а какая ещё ждёт реальную стоимость от провайдера (см.
scripts/reconcile_sms_charges.py — забирает её отдельно, с задержкой,
провайдер не отдаёт стоимость сразу при отправке).

Бэкофилл: заводит новый счёт КАЖДОМУ человеку, у которого сейчас есть хотя
бы одно действующее владение гаражом (т.е. каждому текущему члену
кооператива) — то же самое, что произошло бы у НОВОГО члена автоматически
при добавлении его собственником (см. accounting.ensure_personal_member_accounts,
вызывается оттуда же, где и обычные лицевые счета). Номер счёта — по той
же формуле (тип_кода + id человека, дополненный нулями до ширины
account_number_settings.garage_digits, по умолчанию 3), без обращения к
живым моделям приложения (см. соглашение проекта — school миграций,
напр. d1786b15be12) — переопределение формулы в accounting.py её не
затронет.

Revision ID: 4aafd6b5140a
Revises: 3fcedcde5c0c
Create Date: 2026-09-14 22:46:16.751825

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4aafd6b5140a'
down_revision: Union[str, Sequence[str], None] = '3fcedcde5c0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FEE_TYPE_CODE = "telecom_disputes"
FEE_TYPE_NAME = "Телекоммуникационные услуги и споры"
TYPE_CODE = "3"  # "1" земельный налог, "2" членский взнос (см. seed.py) — следующий свободный


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    with op.batch_alter_table('fee_type', schema=None) as batch_op:
        batch_op.add_column(sa.Column('per_garage', sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table('fee_type', schema=None) as batch_op:
        batch_op.alter_column('per_garage', server_default=None)

    with op.batch_alter_table('sms_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('purpose', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('person_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('provider_message_id', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('charge_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_sms_log_purpose'), ['purpose'], unique=False)
        batch_op.create_index(batch_op.f('ix_sms_log_person_id'), ['person_id'], unique=False)
        batch_op.create_foreign_key(
            batch_op.f('fk_sms_log_person_id_person'), 'person', ['person_id'], ['id'], ondelete='SET NULL',
        )
        batch_op.create_foreign_key(
            batch_op.f('fk_sms_log_charge_id_charge'), 'charge', ['charge_id'], ['id'], ondelete='SET NULL',
        )
        batch_op.create_unique_constraint(batch_op.f('uq_sms_log_charge_id'), ['charge_id'])

    with op.batch_alter_table('member_account', schema=None) as batch_op:
        batch_op.alter_column('garage_id', existing_type=sa.Integer(), nullable=True)

    # Новый вид взноса — если такого code ещё нет (idempotent, тот же приём,
    # что и в других миграциях/seed.py — на случай повторного запуска на
    # уже накатанной БД, напр. после restore).
    existing = conn.execute(sa.text(
        "SELECT id FROM fee_type WHERE code = :code"
    ), {"code": FEE_TYPE_CODE}).fetchone()
    if existing is None:
        result = conn.execute(sa.text(
            "INSERT INTO fee_type (code, name, type_code, is_penalty, per_garage) "
            "VALUES (:code, :name, :type_code, 0, 0)"
        ), {"code": FEE_TYPE_CODE, "name": FEE_TYPE_NAME, "type_code": TYPE_CODE})
        fee_type_id = result.lastrowid
    else:
        fee_type_id = existing[0]

    settings_row = conn.execute(sa.text("SELECT garage_digits FROM account_number_settings LIMIT 1")).fetchone()
    garage_digits = settings_row[0] if settings_row else 3

    person_ids = [
        row[0] for row in conn.execute(sa.text(
            "SELECT DISTINCT person_id FROM garage_ownership"
        )).fetchall()
    ]
    created = 0
    for person_id in person_ids:
        exists = conn.execute(sa.text(
            "SELECT 1 FROM member_account WHERE person_id = :pid AND fee_type_id = :ftid "
            "AND garage_id IS NULL AND is_archived = 0"
        ), {"pid": person_id, "ftid": fee_type_id}).fetchone()
        if exists is not None:
            continue
        account_number = f"{TYPE_CODE}{str(person_id).zfill(garage_digits)}"
        conn.execute(sa.text(
            "INSERT INTO member_account (person_id, garage_id, fee_type_id, account_number, opened_date, is_archived) "
            "VALUES (:pid, NULL, :ftid, :num, date('now'), 0)"
        ), {"pid": person_id, "ftid": fee_type_id, "num": account_number})
        created += 1

    print(f"Вид взноса «{FEE_TYPE_NAME}»: заведено личных счетов — {created} из {len(person_ids)} текущих членов кооператива.")


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()

    # garage_id снова NOT NULL ниже — счета без гаража (заведённые upgrade()
    # выше для telecom_disputes) этому уже не удовлетворяют, надо убрать их
    # ДО возврата ограничения, иначе copy-in при пересборке таблицы (SQLite
    # batch-режим) упадёт на NOT NULL constraint failed. Это единственная
    # миграция в проекте, где downgrade всё же удаляет данные (обычно —
    # только схему, см. др. миграции), т.к. иначе откатить схему буквально
    # нельзя: сами эти счета осмысленны только без гаража.
    conn.execute(sa.text(
        "DELETE FROM charge WHERE account_id IN (SELECT id FROM member_account WHERE garage_id IS NULL)"
    ))
    conn.execute(sa.text(
        "DELETE FROM payment WHERE account_id IN (SELECT id FROM member_account WHERE garage_id IS NULL)"
    ))
    conn.execute(sa.text("DELETE FROM member_account WHERE garage_id IS NULL"))
    conn.execute(sa.text("DELETE FROM fee_type WHERE code = :code"), {"code": FEE_TYPE_CODE})

    with op.batch_alter_table('member_account', schema=None) as batch_op:
        batch_op.alter_column('garage_id', existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table('sms_log', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('uq_sms_log_charge_id'), type_='unique')
        batch_op.drop_constraint(batch_op.f('fk_sms_log_charge_id_charge'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_sms_log_person_id_person'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_sms_log_person_id'))
        batch_op.drop_index(batch_op.f('ix_sms_log_purpose'))
        batch_op.drop_column('charge_id')
        batch_op.drop_column('provider_message_id')
        batch_op.drop_column('person_id')
        batch_op.drop_column('purpose')

    with op.batch_alter_table('fee_type', schema=None) as batch_op:
        batch_op.drop_column('per_garage')
    # Примечание: downgrade НЕ удаляет ни сам FeeType "telecom_disputes", ни
    # заведённые для него личные счета (в т.ч. с уже проведёнными по ним
    # начислениями/платежами) — как и в остальных миграциях проекта, откат
    # чистит только схему, не бизнес-данные, которые могли накопиться после
    # применения. Если нужно избавиться от них совсем — вручную через UI.
