"""fix garage 37 owner

Гараж № 37 целиком принадлежит Дедюлину Сергею Александровичу.
Дедюлина Елена Генадьевна числилась совладельцем (1/2) только для
проверки функционала — с неверными данными: начисления по гаражу делились
пополам, половину оплаты Сергея за 2025 год зачли на её счета, а при
тестовом удалении Сергея из собственников его долг перенёсся на неё.

Исправление (только данные, схема не меняется):
- убираются взаимные зачёты между их счетами по гаражу и записи переноса
  остатка при выбытии собственника («Долг/Переплата передан(а) остальным
  собственникам…», «Доля долга/переплаты выбывшего собственника (…)»);
- начисления и платежи со счетов Елены по гаражу переходят на счета
  Сергея того же вида взноса: массовое начисление за тот же год
  складывается с его начислением (одно начисление за год на весь гараж),
  остальное просто переносится; счета Елены по гаражу удаляются;
- собственник — только Сергей, доля 1; события владения по гаражу,
  оставшиеся от проверки, удаляются;
- разнесение платежей по начислениям (charge_allocation) на счетах Сергея
  по гаражу пересчитывается (FIFO, как accounting.reallocate_member_charges).

Елена остаётся лицом для связи по гаражу (garage_contact, «жена»); её
карточка (person) и счета, не привязанные к гаражу, не трогаются.
Если в базе нет гаража № 37 или кого-то из двоих — ничего не делается.

Revision ID: e9a3d5b7c182
Revises: d8b4e1c6a273
Create Date: 2026-11-17 22:00:00.000000

"""
import datetime as dt
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9a3d5b7c182'
down_revision: Union[str, Sequence[str], None] = 'd8b4e1c6a273'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GARAGE_NUMBER = "37"
OWNER_NAME = "Дедюлин Сергей Александрович"
TEST_NAME = "Дедюлина Елена Генадьевна"

# Записи переноса остатка при выбытии собственника
# (accounting.redistribute_member_account_balance).
TRANSFER_OUT = (
    "Долг передан остальным собственникам при выбытии из числа собственников",
    "Переплата передана остальным собственникам при выбытии из числа собственников",
)
TRANSFER_IN_PREFIXES = ("Доля долга выбывшего собственника (", "Доля переплаты выбывшего собственника (")
MASS_PREFIX = "Массовое начисление"


def _dec(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def upgrade() -> None:
    conn = op.get_bind()

    def rows(sql, **params):
        return conn.execute(sa.text(sql), params).mappings().all()

    def run(sql, **params):
        conn.execute(sa.text(sql), params)

    garage = rows("SELECT id FROM garage WHERE number = :n", n=GARAGE_NUMBER)
    owner = rows("SELECT id FROM person WHERE full_name = :n", n=OWNER_NAME)
    tester = rows("SELECT id FROM person WHERE full_name = :n", n=TEST_NAME)
    if len(garage) != 1 or len(owner) != 1 or len(tester) != 1:
        return
    garage_id, owner_id, tester_id = garage[0]["id"], owner[0]["id"], tester[0]["id"]

    accounts = rows(
        "SELECT id, person_id, fee_type_id, is_archived FROM member_account "
        "WHERE garage_id = :g AND person_id IN (:o, :t)", g=garage_id, o=owner_id, t=tester_id,
    )
    owner_accounts = {}
    for a in sorted(accounts, key=lambda a: (a["is_archived"], a["id"])):
        if a["person_id"] == owner_id:
            owner_accounts.setdefault(a["fee_type_id"], a["id"])
    tester_accounts = [a for a in accounts if a["person_id"] == tester_id]
    owner_ids = set(owner_accounts.values()) | {a["id"] for a in accounts if a["person_id"] == owner_id}
    tester_ids = {a["id"] for a in tester_accounts}
    all_ids = owner_ids | tester_ids
    if not all_ids:
        return
    in_all = ",".join(str(i) for i in sorted(all_ids))

    def delete_charge(charge_id):
        run("DELETE FROM charge_allocation WHERE charge_id = :c", c=charge_id)
        for column in ("offset_charge_id", "amnesty_for_charge_id"):
            run(f"UPDATE payment SET {column} = NULL WHERE {column} = :c", c=charge_id)
        run("UPDATE charge SET penalty_for_charge_id = NULL WHERE penalty_for_charge_id = :c", c=charge_id)
        run("UPDATE sms_log SET charge_id = NULL WHERE charge_id = :c", c=charge_id)
        run("DELETE FROM charge WHERE id = :c", c=charge_id)

    def delete_payment(payment_id):
        run("DELETE FROM charge_allocation WHERE payment_id = :p", p=payment_id)
        run("UPDATE bank_statement_line SET matched_payment_id = NULL WHERE matched_payment_id = :p", p=payment_id)
        run("DELETE FROM payment WHERE id = :p", p=payment_id)

    # 1. Взаимные зачёты между счетами Сергея и Елены по гаражу: платёж-зачёт
    # на одном счёте и его начисление-списание (offset_charge) на другом.
    for p in rows(
        f"SELECT p.id, p.account_id, c.id AS charge_id, c.account_id AS charge_account "
        f"FROM payment p JOIN charge c ON c.id = p.offset_charge_id "
        f"WHERE p.account_id IN ({in_all}) AND c.account_id IN ({in_all})"
    ):
        if {p["account_id"] in tester_ids, p["charge_account"] in tester_ids} == {True, False}:
            delete_payment(p["id"])
            delete_charge(p["charge_id"])

    # 2. Перенос остатка при тестовом выбытии собственника.
    for p in rows(f"SELECT id, comment FROM payment WHERE account_id IN ({in_all})"):
        if (p["comment"] or "") in TRANSFER_OUT or (p["comment"] or "").startswith(TRANSFER_IN_PREFIXES):
            delete_payment(p["id"])
    for c in rows(f"SELECT id, comment FROM charge WHERE account_id IN ({in_all})"):
        if (c["comment"] or "") in TRANSFER_OUT or (c["comment"] or "").startswith(TRANSFER_IN_PREFIXES):
            delete_charge(c["id"])

    # 3. Счета Елены по гаражу → счета Сергея того же вида взноса.
    for account in tester_accounts:
        target = owner_accounts.get(account["fee_type_id"])
        if target is None:
            # У Сергея нет счёта этого вида — счёт просто переходит к нему.
            run("UPDATE member_account SET person_id = :o, is_archived = 0 WHERE id = :a",
                o=owner_id, a=account["id"])
            owner_accounts[account["fee_type_id"]] = account["id"]
            owner_ids.add(account["id"])
            continue
        for c in rows("SELECT id, year, amount, comment FROM charge WHERE account_id = :a", a=account["id"]):
            same = None
            if (c["comment"] or "").startswith(MASS_PREFIX):
                same = rows(
                    "SELECT id, amount FROM charge WHERE account_id = :a AND year = :y AND comment = :cm "
                    "AND penalty_for_charge_id IS NULL ORDER BY id",
                    a=target, y=c["year"], cm=c["comment"],
                )
            if same:
                run("UPDATE charge SET amount = :amt WHERE id = :id",
                    amt=str(_dec(same[0]["amount"]) + _dec(c["amount"])), id=same[0]["id"])
                for column in ("offset_charge_id", "amnesty_for_charge_id"):
                    run(f"UPDATE payment SET {column} = :n WHERE {column} = :c", n=same[0]["id"], c=c["id"])
                run("UPDATE charge SET penalty_for_charge_id = :n WHERE penalty_for_charge_id = :c",
                    n=same[0]["id"], c=c["id"])
                delete_charge(c["id"])
            else:
                run("UPDATE charge SET account_id = :t WHERE id = :c", t=target, c=c["id"])
        run("UPDATE payment SET account_id = :t WHERE account_id = :a", t=target, a=account["id"])
        run("DELETE FROM pd4_document WHERE account_id = :a", a=account["id"])
        run("DELETE FROM member_account WHERE id = :a", a=account["id"])

    # 4. Собственник — только Сергей, целиком.
    run("DELETE FROM garage_ownership WHERE garage_id = :g AND person_id = :t", g=garage_id, t=tester_id)
    if rows("SELECT id FROM garage_ownership WHERE garage_id = :g AND person_id = :o", g=garage_id, o=owner_id):
        run("UPDATE garage_ownership SET share = 1 WHERE garage_id = :g AND person_id = :o", g=garage_id, o=owner_id)
    else:
        run("INSERT INTO garage_ownership (garage_id, person_id, share) VALUES (:g, :o, 1)", g=garage_id, o=owner_id)
    # Елена остаётся лицом для связи по гаражу — жена собственника.
    if not rows("SELECT id FROM garage_contact WHERE garage_id = :g AND person_id = :t", g=garage_id, t=tester_id):
        run("INSERT INTO garage_contact (garage_id, person_id, relation, is_tenant) VALUES (:g, :t, 'жена', 0)",
            g=garage_id, t=tester_id)
    run("DELETE FROM garage_ownership_event WHERE garage_id = :g AND person_id IN (:o, :t)",
        g=garage_id, o=owner_id, t=tester_id)
    run(f"UPDATE member_account SET is_archived = 0 WHERE id IN ({','.join(str(i) for i in owner_ids)})")

    # 5. Разнесение платежей по начислениям на счетах Сергея (FIFO).
    for account_id in owner_ids:
        charges = rows("SELECT id, year, amount FROM charge WHERE account_id = :a ORDER BY id", a=account_id)
        payments = rows("SELECT id, date, amount FROM payment WHERE account_id = :a ORDER BY id", a=account_id)
        run(f"DELETE FROM charge_allocation WHERE charge_id IN (SELECT id FROM charge WHERE account_id = :a) "
            f"OR payment_id IN (SELECT id FROM payment WHERE account_id = :a)", a=account_id)
        charges = sorted(charges, key=lambda c: c["year"])
        payments = sorted(payments, key=lambda p: str(p["date"]))
        queue = [[p["id"], _dec(p["amount"])] for p in payments]
        for c in charges:
            left = _dec(c["amount"])
            while left > 0 and queue:
                if queue[0][1] <= 0:
                    queue.pop(0)
                    continue
                amount = min(left, queue[0][1])
                run("INSERT INTO charge_allocation (charge_id, payment_id, amount) VALUES (:c, :p, :a)",
                    c=c["id"], p=queue[0][0], a=str(amount))
                left -= amount
                queue[0][1] -= amount

    run(
        "INSERT INTO audit_log (created_at, actor_username, action, entity_type, entity_id, summary) "
        "VALUES (:at, 'migration', 'garage_owner.fix', 'garage', :g, :s)",
        at=dt.datetime.utcnow(), g=garage_id,
        s=f"Гараж №{GARAGE_NUMBER}: собственник — {OWNER_NAME} (доля 1). Тестовое совладение "
          f"{TEST_NAME} убрано (она — лицо для связи, жена), её начисления и платежи по гаражу перенесены на счета собственника, "
          f"взаимные зачёты и перенос долга при тестовом выбытии отменены.",
    )


def downgrade() -> None:
    # Исправление данных — обратно не возвращается.
    pass
