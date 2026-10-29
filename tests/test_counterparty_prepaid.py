"""
Расчёты с контрагентом на предоплате (Counterparty.prepaid: SMS Aero, Почта
России) — счёт на оплату (ExpenseKind.INVOICE) не начисление и баланс
расчётов не уменьшает, оплата по нему становится авансом, а с аванса
списывают акты/УПД (ExpenseKind.CHARGE). Плюс баланс на дату для актов
сверки (counterparty_balance(as_of=...)).
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import (
    counterparty_balance, counterparty_amount_to_pay, counterparty_unbilled_usage, expense_paid_amount,
)
from app.models import (
    RoleEnum, Counterparty, CounterpartyApiProvider, CounterpartyPayment, Document, DocumentType, Expense, ExpenseKind,
    ReconciliationAct,
)

from tests.conftest import make_user, login


def make_counterparty(db, name="SMS Aero", **kwargs):
    c = Counterparty(name=name, **kwargs)
    db.add(c)
    db.flush()
    return c


def _login_board(db, client, username):
    make_user(db, username, "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, username, "pass12345")


def _reload(counterparty_id):
    database.db_session.expire_all()
    return database.db_session.get(Counterparty, counterparty_id)


def test_prepaid_invoice_payment_act_flow(app, db, client):
    """Счёт 1000 → баланс 0 (не долг); оплата 1000 → +1000 аванс, счёт
    оплачен; акт 300 → аванс 700, у акта статуса оплаты нет."""
    counterparty = make_counterparty(db, prepaid=True)
    _login_board(db, client, "board_pp1")

    client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-10-01", "amount": "1000.00", "kind": "INVOICE",
    })
    cp = _reload(counterparty.id)
    invoice = cp.expenses[0]
    assert invoice.kind == ExpenseKind.INVOICE
    assert counterparty_balance(cp) == Decimal("0")
    assert counterparty_amount_to_pay(cp) == Decimal("1000.00")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={"date": "2026-10-02", "amount": "1000.00"})
    cp = _reload(counterparty.id)
    assert counterparty_balance(cp) == Decimal("1000.00")
    assert expense_paid_amount(database.db_session.get(Expense, invoice.id)) == Decimal("1000.00")
    assert counterparty_amount_to_pay(cp) == Decimal("0")

    client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-10-31", "amount": "300.00", "kind": "CHARGE",
    })
    cp = _reload(counterparty.id)
    assert counterparty_balance(cp) == Decimal("700.00")
    act = next(e for e in cp.expenses if e.kind == ExpenseKind.CHARGE)
    assert act.allocations == []
    assert expense_paid_amount(database.db_session.get(Expense, invoice.id)) == Decimal("1000.00")

    resp = client.get(f"/counterparties/{counterparty.id}")
    html = resp.get_data(as_text=True)
    assert "аванс у контрагента" in html
    assert "Счета на оплату" in html
    assert "Акты / УПД (начислено" in html


def test_prepaid_empty_payment_amount_pays_unpaid_invoices(app, db, client):
    counterparty = make_counterparty(db, prepaid=True)
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 10, 1), amount=Decimal("450.00"), kind=ExpenseKind.INVOICE))
    _login_board(db, client, "board_pp2")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={"date": "2026-10-02", "amount": ""})
    assert resp.status_code == 302
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert payment.amount == Decimal("450.00")
    assert counterparty_balance(_reload(counterparty.id)) == Decimal("450.00")


def test_postpaid_ignores_kind_from_form(app, db, client):
    """ТНС Энерго и прочие на постоплате: счёт приходит с УПД — всегда
    начисление, даже если в POST подсунули kind=INVOICE."""
    counterparty = make_counterparty(db, name="ТНС Энерго")
    _login_board(db, client, "board_pp3")

    client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-10-01", "amount": "2000.00", "kind": "INVOICE",
    })
    cp = _reload(counterparty.id)
    assert cp.expenses[0].kind == ExpenseKind.CHARGE
    assert counterparty_balance(cp) == Decimal("-2000.00")
    assert counterparty_amount_to_pay(cp) == Decimal("2000.00")

    html = client.get(f"/counterparties/{counterparty.id}").get_data(as_text=True)
    assert "Расходы (начислено)" in html
    assert "Счета на оплату" not in html


def test_cannot_unset_prepaid_with_invoices(app, db, client):
    counterparty = make_counterparty(db, prepaid=True)
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 10, 1), amount=Decimal("100.00"), kind=ExpenseKind.INVOICE))
    _login_board(db, client, "board_pp4")

    client.post(f"/counterparties/{counterparty.id}/edit", data={"name": "SMS Aero"})
    assert _reload(counterparty.id).prepaid is True


def test_turning_prepaid_on_and_moving_to_invoices(app, db, client):
    """Старые «счета-начисления» постоплатного контрагента: включили
    предоплату, перенесли строку в счета — платёж разносится по счёту, тип
    прикреплённого документа меняется на «Счёт». Правка формы вид не меняет."""
    counterparty = make_counterparty(db)
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 10, 1), amount=Decimal("500.00"))
    db.add(expense)
    db.flush()
    doc = Document(
        doc_type=DocumentType.UTD, date=dt.date(2026, 10, 1), title="Счёт", file_path="x.pdf",
        counterparty_id=counterparty.id, expense_id=expense.id, is_internal=True,
    )
    db.add(doc)
    db.add(CounterpartyPayment(counterparty_id=counterparty.id, date=dt.date(2026, 10, 2), amount=Decimal("500.00")))
    _login_board(db, client, "board_pp5")

    client.post(f"/counterparties/{counterparty.id}/edit", data={"name": "SMS Aero", "prepaid": "1"})
    client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-10-01", "amount": "500.00", "kind": "INVOICE",
    })
    assert database.db_session.get(Expense, expense.id).kind == ExpenseKind.CHARGE

    resp = client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/move")
    assert resp.status_code == 302
    cp = _reload(counterparty.id)
    assert cp.prepaid is True
    assert database.db_session.get(Expense, expense.id).kind == ExpenseKind.INVOICE
    assert database.db_session.get(Document, doc.id).doc_type == DocumentType.INVOICE
    assert counterparty_balance(cp) == Decimal("500.00")
    assert expense_paid_amount(database.db_session.get(Expense, expense.id)) == Decimal("500.00")

    client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/move")
    database.db_session.expire_all()
    assert database.db_session.get(Expense, expense.id).kind == ExpenseKind.CHARGE
    assert database.db_session.get(Document, doc.id).doc_type == DocumentType.UTD


def test_move_not_allowed_for_postpaid(app, db, client):
    counterparty = make_counterparty(db, name="ТНС Энерго")
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 10, 1), amount=Decimal("500.00"))
    db.add(expense)
    _login_board(db, client, "board_pp7")

    assert client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/move").status_code == 400


def test_unbilled_usage_against_lk_balance(app, db):
    counterparty = make_counterparty(
        db, prepaid=True, api_provider=CounterpartyApiProvider.SMSAERO, external_balance=Decimal("500.00"),
    )
    db.add(CounterpartyPayment(counterparty_id=counterparty.id, date=dt.date(2026, 10, 2), amount=Decimal("1000.00")))
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 10, 31), amount=Decimal("300.00")))
    db.flush()
    cp = _reload(counterparty.id)
    assert counterparty_unbilled_usage(cp) == Decimal("200.00")

    cp.prepaid = False
    assert counterparty_unbilled_usage(cp) is None


def test_balance_as_of_and_reconciliation_default(app, db, client):
    """Акт сверки (ТНС Энерго, раз в квартал): «наш баланс» по умолчанию —
    на конец периода, а не на сегодня."""
    counterparty = make_counterparty(
        db, name="ТНС Энерго", opening_balance=Decimal("-100.00"), opening_balance_date=dt.date(2026, 1, 1),
    )
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 3, 10), amount=Decimal("1000.00")))
    db.add(CounterpartyPayment(counterparty_id=counterparty.id, date=dt.date(2026, 3, 20), amount=Decimal("1100.00")))
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 4, 10), amount=Decimal("700.00")))
    _login_board(db, client, "board_pp6")

    cp = _reload(counterparty.id)
    assert counterparty_balance(cp, as_of=dt.date(2025, 12, 31)) == Decimal("0")
    assert counterparty_balance(cp, as_of=dt.date(2026, 3, 31)) == Decimal("0.00")
    assert counterparty_balance(cp) == Decimal("-700.00")

    resp = client.get(f"/counterparties/{counterparty.id}/balance?as_of=2026-03-15")
    assert resp.get_json() == {"balance": "-1100.00"}

    client.post(f"/counterparties/{counterparty.id}/reconciliation/new", data={
        "period_start": "2026-01-01", "period_end": "2026-03-31", "counterparty_balance": "0",
    })
    act = db.query(ReconciliationAct).filter_by(counterparty_id=counterparty.id).one()
    assert act.our_balance == Decimal("0.00")
