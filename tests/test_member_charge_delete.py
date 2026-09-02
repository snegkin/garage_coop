"""
Удаление начисления (finance.delete_member_charge) — по кнопке рядом с
«Изменить» на странице лицевого счёта. Доступ — CHAIRMAN (как у
delete_member_payment, не is_board(), как у создания/правки начисления).
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import balance
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_account(db, account_number="19701", garage_number="97", fee_type_code="membership"):
    person = make_person(db, full_name="Списанов Семён Семёнович")
    garage = make_garage(db, number=garage_number)
    make_ownership(db, garage, person)
    fee_type = FeeType(code=fee_type_code, name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=account_number)
    db.add(account)
    db.flush()
    return account


def test_delete_member_charge_removes_it_and_reallocates(app, db, client):
    account = _setup_account(db)
    charge = Charge(account_id=account.id, year=2026, amount=Decimal("1710.00"))
    db.add(charge)
    db.add(Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("1710.00")))
    make_user(db, "chair70", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair70", "pass12345")

    assert balance(account) == Decimal("0.00")
    db.expire_all()  # иначе reallocate внутри роута увидит протухшие account.charges/payments

    resp = client.post(f"/finance/member-accounts/{account.id}/charges/{charge.id}/delete")
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(Charge, charge.id) is None
    assert balance(account) == Decimal("1710.00")  # платёж остался, начисления больше нет — переплата


def test_delete_member_charge_requires_chairman(db, client):
    account = _setup_account(db)
    charge = Charge(account_id=account.id, year=2026, amount=Decimal("500.00"))
    db.add(charge)
    person = database.db_session.get(MemberAccount, account.id).person
    make_user(db, "board70", "pass12345", role=RoleEnum.BOARD, person=person)
    db.commit()
    login(client, "board70", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/charges/{charge.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Charge, charge.id) is not None  # не удалено


def test_delete_member_charge_rejects_charge_from_another_account(app, db, client):
    account = _setup_account(db, account_number="19702", garage_number="98", fee_type_code="membership_a")
    other_account = _setup_account(db, account_number="19703", garage_number="99", fee_type_code="membership_b")
    charge = Charge(account_id=other_account.id, year=2026, amount=Decimal("500.00"))
    db.add(charge)
    make_user(db, "chair71", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair71", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/charges/{charge.id}/delete")
    assert resp.status_code == 404
    db.expire_all()
    assert database.db_session.get(Charge, charge.id) is not None


def test_delete_member_charge_rejects_transfer_source_charge(app, db, client):
    """Начисление — «источник» зачёта между счетами (Payment.offset_charge_id
    на него ссылается) — прямое удаление заблокировано, нужна отмена зачёта
    со стороны платежа (finance.cancel_transfer)."""
    person = make_person(db, full_name="Зачётнов Захар Захарович")
    garage = make_garage(db, number="100")
    make_ownership(db, garage, person)
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    land_tax = FeeType(code="land_tax", name="Земельный налог", type_code="2")
    db.add_all([membership, land_tax])
    db.flush()
    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="19801")
    target = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="19802")
    db.add_all([source, target])
    db.flush()
    db.add(Payment(account_id=source.id, date=dt.date(2026, 1, 1), amount=Decimal("600.00")))
    make_user(db, "chair72", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair72", "pass12345")

    client.post(f"/finance/member-accounts/{source.id}/transfer", data={
        "target_account_id": str(target.id), "amount": "600.00",
    })
    db.expire_all()
    transfer_charge = db.query(Charge).filter_by(account_id=source.id).one()

    resp = client.post(f"/finance/member-accounts/{source.id}/charges/{transfer_charge.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Charge, transfer_charge.id) is not None  # не удалено
