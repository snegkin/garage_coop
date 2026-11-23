"""
Подарок освобождением от уплаты взносов (app/gifts.py): платёж на лицевом
счёте + расход «Поощрения» у контрагента на имя человека, оплаченный
«зачётом»; лимит 4000 ₽ на человека за год по всем счетам и способам;
отмена — только обеих записей вместе.
"""
import datetime as dt
from decimal import Decimal

from app import gifts
from app.accounting import balance, counterparty_balance
from app.models import (
    Charge, Counterparty, CounterpartyPayment, Expense, FeeType, MemberAccount, Payment, RoleEnum,
)
from app.year_report import owed_people

from tests.conftest import make_person, make_garage, make_ownership, make_user, login

TODAY = dt.date.today()


def _setup(db):
    person = make_person(db, full_name="Активов Пётр Петрович")
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    penalty = FeeType(code="penalty", name="Пеня", type_code="9", is_penalty=True)
    db.add_all([membership, penalty])
    db.flush()
    accounts = []
    for n in ("1", "2"):  # два гаража — два счёта взносов
        garage = make_garage(db, number=f"7{n}")
        make_ownership(db, garage, person)
        account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id,
                                account_number=f"2900{n}")
        db.add(account)
        db.flush()
        db.add(Charge(account_id=account.id, year=TODAY.year, amount=Decimal("3000.00")))
        accounts.append(account)
    fine = MemberAccount(person_id=person.id, garage_id=accounts[0].garage_id, fee_type_id=penalty.id,
                         account_number="29009")
    db.add(fine)
    db.flush()
    db.add(Charge(account_id=fine.id, year=TODAY.year, amount=Decimal("100.00")))
    make_user(db, "chair_gift", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    return person, accounts, fine


def _gift(client, account, amount, decision="протокол № 3 от 20.11.2026"):
    return client.post(f"/finance/member-accounts/{account.id}/gift", data={
        "date": TODAY.isoformat(), "amount": amount, "decision": decision,
    })


def test_gift_settles_dues_and_records_expense(app, db, client):
    person, (first, second), _fine = _setup(db)
    login(client, "chair_gift", "pass12345")
    page = client.get(f"/finance/member-accounts/{first.id}").get_data(as_text=True)
    assert "Подарок в счёт взносов" in page and f"в {TODAY.year} году: <strong>4000,00 ₽" in page

    assert _gift(client, first, "2500").status_code == 302
    db.expire_all()
    assert balance(first) == Decimal("-500.00")
    payment = db.query(Payment).filter_by(account_id=first.id).one()
    expense = db.get(Expense, payment.gift_expense_id)
    assert expense.category == gifts.GIFT_CATEGORY and expense.person_id == person.id
    assert expense.amount == Decimal("2500.00") and "протокол № 3" in expense.description
    settlement = expense.booked_payment
    assert settlement.bank_account_id is None and settlement.comment == gifts.GIFT_SETTLEMENT_COMMENT
    # Долга человеку нет — расход оплачен зачётом.
    assert counterparty_balance(expense.counterparty) == Decimal("0")
    with app.test_request_context():
        assert not any(r["person"] and r["person"].id == person.id for r in owed_people())

    # Лимит — на человека: на второй счёт осталось 1500 ₽.
    _gift(client, second, "2000")
    db.expire_all()
    assert balance(second) == Decimal("-3000.00")
    assert _gift(client, second, "1500").status_code == 302
    db.expire_all()
    assert balance(second) == Decimal("-1500.00")
    assert gifts.gifts_total(person, TODAY.year) == Decimal("4000.00")


def test_gift_limits(app, db, client):
    _person, (first, _second), fine = _setup(db)
    login(client, "chair_gift", "pass12345")
    _gift(client, first, "3500")  # больше долга по счёту
    _gift(client, first, "1000", decision="  ")  # без решения правления
    _gift(client, fine, "100")  # пеня подарком не закрывается
    db.expire_all()
    assert db.query(Payment).count() == 0 and db.query(Expense).count() == 0


def test_cash_gift_counts_toward_limit(app, db, client):
    person, (first, _second), _fine = _setup(db)
    login(client, "chair_gift", "pass12345")
    client.post("/year-report/expenses/new", data={
        "date": TODAY.isoformat(), "amount": "3000", "mode": "accrued", "person_id": person.id,
        "category": gifts.GIFT_CATEGORY, "description": "Поощрение",
    })
    db.expire_all()
    assert gifts.gifts_total(person, TODAY.year) == Decimal("3000.00")
    _gift(client, first, "1500")
    db.expire_all()
    assert db.query(Payment).count() == 0
    # Сверх лимита и деньгами не дарится.
    client.post("/year-report/expenses/new", data={
        "date": TODAY.isoformat(), "amount": "1500", "mode": "accrued", "person_id": person.id,
        "category": gifts.GIFT_CATEGORY,
    })
    db.expire_all()
    assert gifts.gifts_total(person, TODAY.year) == Decimal("3000.00")


def test_cancel_gift_removes_both_sides(app, db, client):
    person, (first, _second), _fine = _setup(db)
    login(client, "chair_gift", "pass12345")
    _gift(client, first, "1000")
    db.expire_all()
    payment = db.query(Payment).filter_by(account_id=first.id).one()
    expense = db.get(Expense, payment.gift_expense_id)
    counterparty_id = expense.counterparty_id

    # По отдельности не удаляются.
    client.post(f"/finance/member-accounts/{first.id}/payments/{payment.id}/delete")
    client.post(f"/counterparties/{counterparty_id}/expenses/{expense.id}/delete")
    client.post(f"/counterparties/{counterparty_id}/payments/{expense.booked_payment_id}/reverse")
    db.expire_all()
    assert db.query(Payment).count() == 1 and db.query(Expense).count() == 1
    assert db.query(CounterpartyPayment).count() == 1

    assert client.post(f"/finance/member-accounts/{first.id}/payments/{payment.id}/cancel-gift").status_code == 302
    db.expire_all()
    assert db.query(Payment).count() == 0 and db.query(Expense).count() == 0
    assert db.query(CounterpartyPayment).count() == 0
    assert balance(db.get(MemberAccount, first.id)) == Decimal("-3000.00")
    assert counterparty_balance(db.get(Counterparty, counterparty_id)) == Decimal("0")
    assert gifts.gifts_total(person, TODAY.year) == Decimal("0")
