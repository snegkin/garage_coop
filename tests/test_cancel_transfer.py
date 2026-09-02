"""
Отмена зачёта между счетами (finance.cancel_transfer) — зачёт создаёт две
связанные половины (Charge на счёте-источнике, Payment на счёте-
получателе с Payment.offset_charge_id на этот Charge, см.
finance.transfer_member_account_funds). Обычное «Удалить платёж»
(finance.delete_member_payment) на такой платёж не действует — деньги не
должны молча "теряться" для владельца счёта-источника при удалении
только одной половины.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import balance
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, AuditLog

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_fee_types(db):
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    land_tax = FeeType(code="land_tax", name="Земельный налог", type_code="2")
    db.add_all([membership, land_tax])
    db.flush()
    return membership, land_tax


def test_cancel_transfer_removes_both_halves_and_restores_balances(app, db, client):
    person = make_person(db, full_name="Отменов Олег Олегович")
    garage = make_garage(db, number="90")
    make_ownership(db, garage, person)
    membership, land_tax = _setup_fee_types(db)

    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="19001")
    target = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="19002")
    db.add_all([source, target])
    db.flush()
    db.add(Payment(account_id=source.id, date=dt.date(2026, 1, 1), amount=Decimal("500.00")))  # переплата на источнике

    make_user(db, "chair60", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair60", "pass12345")

    resp = client.post(f"/finance/member-accounts/{source.id}/transfer", data={
        "target_account_id": str(target.id),
        "amount": "500.00",
    })
    assert resp.status_code == 302
    db.expire_all()

    assert balance(source) == Decimal("0.00")  # переплата зачтена
    assert balance(target) == Decimal("500.00")
    transfer_payment = db.query(Payment).filter_by(account_id=target.id).one()
    transfer_charge = db.query(Charge).filter_by(account_id=source.id).one()
    assert transfer_payment.offset_charge_id == transfer_charge.id
    db.expire_all()  # иначе reallocate внутри роута увидит протухшие account.charges/payments

    resp2 = client.post(f"/finance/member-accounts/{target.id}/payments/{transfer_payment.id}/cancel-transfer")
    assert resp2.status_code == 302
    db.expire_all()

    # обе половины удалены, оба счёта вернулись к состоянию до зачёта
    assert db.query(Payment).filter_by(id=transfer_payment.id).first() is None
    assert db.query(Charge).filter_by(id=transfer_charge.id).first() is None
    assert balance(source) == Decimal("500.00")
    assert balance(target) == Decimal("0.00")

    entries = db.query(AuditLog).filter_by(action="member_account.transfer_cancel").all()
    assert len(entries) == 1


def test_delete_member_payment_rejects_transfer_payment(app, db, client):
    """finance.delete_member_payment отказывается удалять платёж-половину
    зачёта — направляет использовать cancel_transfer вместо себя."""
    person = make_person(db, full_name="Заблоков Захар Захарович")
    garage = make_garage(db, number="91")
    make_ownership(db, garage, person)
    membership, land_tax = _setup_fee_types(db)

    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="19101")
    target = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="19102")
    db.add_all([source, target])
    db.flush()
    db.add(Payment(account_id=source.id, date=dt.date(2026, 1, 1), amount=Decimal("300.00")))

    make_user(db, "chair61", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair61", "pass12345")

    client.post(f"/finance/member-accounts/{source.id}/transfer", data={
        "target_account_id": str(target.id), "amount": "300.00",
    })
    db.expire_all()
    transfer_payment = db.query(Payment).filter_by(account_id=target.id).one()

    resp = client.post(f"/finance/member-accounts/{target.id}/payments/{transfer_payment.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    # платёж НЕ удалён обычным способом
    assert db.query(Payment).filter_by(id=transfer_payment.id).first() is not None


def test_cancel_transfer_rejects_non_transfer_payment(app, db, client):
    person = make_person(db, full_name="Обычнов Олег Иванович")
    garage = make_garage(db, number="92")
    make_ownership(db, garage, person)
    membership, _land_tax = _setup_fee_types(db)
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="19201")
    db.add(account)
    db.flush()
    payment = Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("200.00"))
    db.add(payment)
    make_user(db, "chair62", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair62", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/payments/{payment.id}/cancel-transfer")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(Payment).filter_by(id=payment.id).first() is not None  # не удалён


def test_cancel_transfer_requires_chairman(app, db, client):
    person = make_person(db, full_name="Правленов Павел Павлович")
    garage = make_garage(db, number="93")
    make_ownership(db, garage, person)
    membership, land_tax = _setup_fee_types(db)
    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="19301")
    target = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="19302")
    db.add_all([source, target])
    db.flush()
    db.add(Payment(account_id=source.id, date=dt.date(2026, 1, 1), amount=Decimal("400.00")))
    make_user(db, "chair63", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair63", "pass12345")

    client.post(f"/finance/member-accounts/{source.id}/transfer", data={
        "target_account_id": str(target.id), "amount": "400.00",
    })
    db.expire_all()
    transfer_payment = db.query(Payment).filter_by(account_id=target.id).one()

    # выходим из-под председателя, заходим правлением (BOARD)
    client.get("/auth/logout")
    make_user(db, "board60", "pass12345", role=RoleEnum.BOARD, person=person)
    db.commit()
    login(client, "board60", "pass12345")

    resp = client.post(f"/finance/member-accounts/{target.id}/payments/{transfer_payment.id}/cancel-transfer")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(Payment).filter_by(id=transfer_payment.id).first() is not None  # не удалён
