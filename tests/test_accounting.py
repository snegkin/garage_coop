"""
Тесты на FIFO-разнесение платежей (accounting._reallocate_fifo и обёртки
reallocate_garage_charges/reallocate_member_charges) — это отмеченный в
context.md как non-negotiable инвариант проекта, ломать его нельзя молча.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import (
    reallocate_garage_charges, reallocate_member_charges, balance, charge_paid_amount,
)
from app.models import Charge, Payment, MemberAccount, FeeType, Person, Garage

from tests.conftest import make_garage, make_person


def test_single_payment_fully_covers_single_charge(app, db):
    garage = make_garage(db)
    charge = Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00"))
    payment = Payment(garage_id=garage.id, date=dt.date(2026, 1, 10), amount=Decimal("1000.00"))
    db.add_all([charge, payment])
    db.flush()

    reallocate_garage_charges(garage)
    db.commit()

    assert charge_paid_amount(charge) == Decimal("1000.00")
    assert balance(garage) == Decimal("0.00")


def test_older_charge_closed_before_newer_one(app, db):
    """FIFO: платёж должен закрывать более раннее начисление первым,
    даже если оба созданы в БД в произвольном порядке."""
    garage = make_garage(db)
    charge_old = Charge(garage_id=garage.id, year=2025, amount=Decimal("500.00"))
    charge_new = Charge(garage_id=garage.id, year=2026, amount=Decimal("500.00"))
    db.add_all([charge_new, charge_old])  # намеренно в обратном порядке
    db.flush()

    payment = Payment(garage_id=garage.id, date=dt.date(2026, 3, 1), amount=Decimal("500.00"))
    db.add(payment)
    db.flush()

    reallocate_garage_charges(garage)
    db.commit()

    assert charge_paid_amount(charge_old) == Decimal("500.00")
    assert charge_paid_amount(charge_new) == Decimal("0.00")


def test_partial_payment_leaves_charge_partially_open(app, db):
    garage = make_garage(db)
    charge = Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00"))
    payment = Payment(garage_id=garage.id, date=dt.date(2026, 1, 10), amount=Decimal("400.00"))
    db.add_all([charge, payment])
    db.flush()

    reallocate_garage_charges(garage)
    db.commit()

    assert charge_paid_amount(charge) == Decimal("400.00")
    assert balance(garage) == Decimal("-600.00")  # долг


def test_overpayment_carries_over_to_next_charge(app, db):
    garage = make_garage(db)
    charge1 = Charge(garage_id=garage.id, year=2025, amount=Decimal("300.00"))
    charge2 = Charge(garage_id=garage.id, year=2026, amount=Decimal("300.00"))
    payment = Payment(garage_id=garage.id, date=dt.date(2026, 1, 1), amount=Decimal("500.00"))
    db.add_all([charge1, charge2, payment])
    db.flush()

    reallocate_garage_charges(garage)
    db.commit()

    assert charge_paid_amount(charge1) == Decimal("300.00")
    assert charge_paid_amount(charge2) == Decimal("200.00")
    assert balance(garage) == Decimal("-100.00")  # 500 - 600, ещё должны 100


def test_reallocation_is_idempotent(app, db):
    """Повторный вызов reallocate_* не должен задваивать разнесение."""
    garage = make_garage(db)
    charge = Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00"))
    payment = Payment(garage_id=garage.id, date=dt.date(2026, 1, 10), amount=Decimal("1000.00"))
    db.add_all([charge, payment])
    db.flush()

    reallocate_garage_charges(garage)
    db.commit()
    reallocate_garage_charges(garage)
    db.commit()
    reallocate_garage_charges(garage)
    db.commit()

    assert charge_paid_amount(charge) == Decimal("1000.00")
    assert balance(garage) == Decimal("0.00")


def test_member_account_fifo_same_as_garage(app, db):
    """reallocate_member_charges — та же механика, для MemberAccount вместо Garage."""
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10001",
    )
    db.add(account)
    db.flush()

    charge_old = Charge(account_id=account.id, year=2025, amount=Decimal("200.00"))
    charge_new = Charge(account_id=account.id, year=2026, amount=Decimal("200.00"))
    payment = Payment(account_id=account.id, date=dt.date(2026, 6, 1), amount=Decimal("250.00"))
    db.add_all([charge_old, charge_new, payment])
    db.flush()

    reallocate_member_charges(account)
    db.commit()

    assert charge_paid_amount(charge_old) == Decimal("200.00")
    assert charge_paid_amount(charge_new) == Decimal("50.00")
    assert balance(account) == Decimal("-150.00")
