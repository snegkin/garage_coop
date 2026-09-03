"""
Кнопка «Пеня» (Списать/Удалить) на карточке персоны (persons/detail.html) —
применяет то же самое, что write_off_penalty/delete_member_charge делают
для одного счёта, сразу ко всем пенным счетам человека одним нажатием.
См. finance.write_off_person_penalties / finance.delete_person_penalties.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, AuditLog

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_person_with_penalties(db, unpaid=("100.00", "50.00"), paid=()):
    """Человек с несколькими пенными счетами (разные виды взноса — на одном
    гараже можно завести несколько MemberAccount только с разными
    fee_type_id) — часть с непогашенным остатком (unpaid — суммы начислений
    без платежей), часть уже закрыта (paid — начислена и сразу оплачена,
    баланс 0)."""
    person = make_person(db, full_name="Пенёв Пётр Пенёвич")
    garage = make_garage(db, number="80")
    make_ownership(db, garage, person)
    accounts = []
    for i, amount in enumerate(unpaid):
        fee_type = FeeType(code=f"penalty_unpaid_{i}", name="Пеня по взносу", type_code=str(i), is_penalty=True)
        db.add(fee_type)
        db.flush()
        account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=f"П801{i}")
        db.add(account)
        db.flush()
        db.add(Charge(account_id=account.id, year=2026, amount=Decimal(amount)))
        accounts.append(account)
    for i, amount in enumerate(paid):
        fee_type = FeeType(code=f"penalty_paid_{i}", name="Пеня по взносу", type_code=f"p{i}", is_penalty=True)
        db.add(fee_type)
        db.flush()
        account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=f"П802{i}")
        db.add(account)
        db.flush()
        db.add(Charge(account_id=account.id, year=2026, amount=Decimal(amount)))
        db.add(Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal(amount)))
        accounts.append(account)
    db.flush()
    return person, accounts


def test_write_off_person_penalties_writes_off_all_unpaid(app, db, client):
    person, accounts = _setup_person_with_penalties(db)
    make_user(db, "chair80", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair80", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/write-off-penalties", data={"reason": "Мировое соглашение"})
    assert resp.status_code == 302
    db.expire_all()

    payments = db.query(Payment).join(MemberAccount).filter(MemberAccount.person_id == person.id).all()
    assert len(payments) == 2
    assert {p.amount for p in payments} == {Decimal("100.00"), Decimal("50.00")}
    assert all("Мировое соглашение" in p.comment for p in payments)


def test_write_off_person_penalties_skips_already_paid_accounts(app, db, client):
    person, accounts = _setup_person_with_penalties(db, unpaid=("100.00",), paid=("30.00",))
    make_user(db, "chair81", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair81", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/write-off-penalties", data={"reason": "Отказ от взыскания"})
    assert resp.status_code == 302
    db.expire_all()

    payments = db.query(Payment).join(MemberAccount).filter(MemberAccount.person_id == person.id).all()
    # Один платёж от начисления (paid), один — от списания (unpaid)
    assert len(payments) == 2
    write_off_payments = [p for p in payments if "Списание пени" in (p.comment or "")]
    assert len(write_off_payments) == 1
    assert write_off_payments[0].amount == Decimal("100.00")


def test_write_off_person_penalties_requires_privileged(app, db, client):
    person, accounts = _setup_person_with_penalties(db)
    make_user(db, "board80", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board80", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/write-off-penalties", data={"reason": "Пытаюсь списать"})
    assert resp.status_code == 403
    db.expire_all()
    assert db.query(Payment).join(MemberAccount).filter(MemberAccount.person_id == person.id).count() == 0


def test_write_off_person_penalties_rejected_without_reason(app, db, client):
    person, accounts = _setup_person_with_penalties(db)
    make_user(db, "chair82", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair82", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/write-off-penalties", data={"reason": ""})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(Payment).join(MemberAccount).filter(MemberAccount.person_id == person.id).count() == 0


def test_delete_person_penalties_deletes_all_charges(app, db, client):
    person, accounts = _setup_person_with_penalties(db)
    make_user(db, "chair83", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair83", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/delete-penalties")
    assert resp.status_code == 302
    db.expire_all()

    remaining = db.query(Charge).filter(Charge.account_id.in_([a.id for a in accounts])).count()
    assert remaining == 0
    entries = db.query(AuditLog).filter_by(action="penalty.delete_all").all()
    assert len(entries) == 1


def test_delete_person_penalties_requires_chairman(app, db, client):
    person, accounts = _setup_person_with_penalties(db)
    make_user(db, "acc80", "pass12345", role=RoleEnum.ACCOUNTANT)
    db.commit()
    login(client, "acc80", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/delete-penalties")
    assert resp.status_code == 302
    db.expire_all()
    remaining = db.query(Charge).filter(Charge.account_id.in_([a.id for a in accounts])).count()
    assert remaining == 2  # не удалено


def test_delete_person_penalties_skips_transfer_source_charge(app, db, client):
    """Защита от порчи зачёта между счетами — начисление, на которое ссылается
    Payment.offset_charge_id, не удаляется даже если технически лежит на
    пенном счету (в реальности зачёт заводится только между обычными
    счетами, но проверка должна отрабатывать вне зависимости от вида счёта)."""
    person, accounts = _setup_person_with_penalties(db, unpaid=("100.00", "50.00"))
    linked_charge = accounts[0].charges[0]
    other_person = make_person(db, full_name="Другой Собственник")
    other_garage = make_garage(db, number="81")
    make_ownership(db, other_garage, other_person)
    other_fee_type = FeeType(code="membership_other", name="Членский взнос")
    db.add(other_fee_type)
    db.flush()
    other_account = MemberAccount(person_id=other_person.id, garage_id=other_garage.id, fee_type_id=other_fee_type.id, account_number="99001")
    db.add(other_account)
    db.flush()
    db.add(Payment(account_id=other_account.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00"), offset_charge_id=linked_charge.id))
    make_user(db, "chair84", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair84", "pass12345")

    resp = client.post(f"/finance/persons/{person.id}/delete-penalties")
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(Charge, linked_charge.id) is not None  # не удалено
    remaining = db.query(Charge).filter(Charge.account_id.in_([a.id for a in accounts])).count()
    assert remaining == 1  # второе (несвязанное) удалено


def test_persons_detail_shows_penalty_dropdown_only_when_unpaid(app, db, client):
    person, accounts = _setup_person_with_penalties(db)
    make_user(db, "chair85", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair85", "pass12345")

    resp = client.get(f"/persons/{person.id}")
    assert resp.status_code == 200
    assert "writeOffAllPenaltiesModal" in resp.get_data(as_text=True)

    # Полностью погашаем обе пени — кнопка должна пропасть
    for acc in accounts:
        db.add(Payment(account_id=acc.id, date=dt.date(2026, 1, 1), amount=acc.charges[0].amount))
    db.commit()
    from app.accounting import reallocate_member_charges
    for acc in accounts:
        reallocate_member_charges(acc)
    db.commit()

    resp = client.get(f"/persons/{person.id}")
    assert resp.status_code == 200
    assert "writeOffAllPenaltiesModal" not in resp.get_data(as_text=True)
