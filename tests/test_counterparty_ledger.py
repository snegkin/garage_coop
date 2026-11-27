"""История баланса расчётов с контрагентом (accounting.counterparty_ledger)
и её вывод на карточке контрагента, плюс история баланса личного кабинета."""
import datetime as dt
from decimal import Decimal

from app.accounting import counterparty_ledger, counterparty_balance
from app.models import (
    Counterparty, CounterpartyPayment, Expense, ExpenseKind, RoleEnum,
    CounterpartyApiProvider, CounterpartyBalanceSnapshot,
)

from tests.conftest import make_person, make_user, login


def _cp(db, **kw):
    c = Counterparty(name="ООО Ромашка", **kw)
    db.add(c)
    db.flush()
    return c


def test_ledger_running_balance_matches_counterparty_balance(db):
    c = _cp(db, opening_balance=Decimal("-100.00"), opening_balance_date=dt.date(2026, 1, 1))
    db.add_all([
        Expense(counterparty_id=c.id, date=dt.date(2026, 2, 1), amount=Decimal("300.00"), category="снег"),
        Expense(counterparty_id=c.id, date=dt.date(2026, 2, 5), amount=Decimal("999.00"), kind=ExpenseKind.INVOICE),
        CounterpartyPayment(counterparty_id=c.id, date=dt.date(2026, 2, 1), amount=Decimal("250.00")),
        CounterpartyPayment(counterparty_id=c.id, date=dt.date(2026, 3, 1), amount=Decimal("150.00")),
    ])
    db.flush()
    db.refresh(c)
    p = c.payments[1]
    db.add(CounterpartyPayment(counterparty_id=c.id, date=dt.date(2026, 3, 2), amount=Decimal("-150.00"), reverses_payment_id=p.id))
    db.flush()
    db.refresh(c)

    rows = counterparty_ledger(c)
    assert [r["kind"] for r in rows] == ["opening", "charge", "payment", "payment", "reversal"]
    # в один день начисление раньше платежа
    assert [r["balance"] for r in rows] == [
        Decimal("-100.00"), Decimal("-400.00"), Decimal("-150.00"), Decimal("0.00"), Decimal("-150.00"),
    ]
    assert rows[-1]["balance"] == counterparty_balance(c)
    assert rows[1]["description"] == "снег"


def test_ledger_empty_without_operations(db):
    assert counterparty_ledger(_cp(db)) == []


def test_detail_page_shows_both_histories(db, client):
    person = make_person(db, full_name="Board One")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=person)
    c = _cp(db, api_provider=CounterpartyApiProvider.SMSAERO,
            external_balance=Decimal("450.50"), external_balance_updated_at=dt.datetime(2026, 11, 2, 4, 0))
    db.add_all([
        Expense(counterparty_id=c.id, date=dt.date(2026, 2, 1), amount=Decimal("300.00")),
        CounterpartyBalanceSnapshot(counterparty_id=c.id, recorded_at=dt.datetime(2026, 11, 1, 4, 0), amount=Decimal("500.00")),
        CounterpartyBalanceSnapshot(counterparty_id=c.id, recorded_at=dt.datetime(2026, 11, 2, 4, 0), amount=Decimal("450.50")),
    ])
    db.commit()
    login(client, "board1", "pass1234")
    body = client.get(f"/counterparties/{c.id}").get_data(as_text=True)

    assert 'id="ledgerTable"' in body
    assert 'id="externalBalanceHistoryTable"' in body
    assert "История баланса расчётов" in body
    assert "-49,50" in body or "-49.50" in body  # изменение баланса ЛК
