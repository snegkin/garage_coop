"""
Правка расхода контрагента (app/counterparties.py: edit_expense).

В отличие от платежей (см. tests/test_counterparty_payment.py:
edit_payment — только последний платёж, т.к. правка задним числом
трогает уже списанный баланс банковского счёта), расход можно
редактировать в любом порядке: Expense не влияет на баланс BankAccount,
а разнесение платежей по расходам (accounting.reallocate_counterparty_expenses)
пересчитывается заново при каждом изменении.
"""
import datetime as dt
import io
from decimal import Decimal

from app.models import RoleEnum, Counterparty, Expense, CounterpartyPayment, AuditLog, DocumentType

from tests.conftest import make_user, login


def make_counterparty(db, name="ООО Ромашка", **kwargs):
    c = Counterparty(name=name, **kwargs)
    db.add(c)
    db.flush()
    return c


def test_board_can_edit_expense(db, client):
    counterparty = make_counterparty(db)
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 5), amount=Decimal("1000.00"), category="снег")
    db.add(expense)
    make_user(db, "board60", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board60", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-02-10", "amount": "1500.00", "category": "уборка", "description": "исправлено",
    })
    assert resp.status_code == 302
    db.expire_all()

    edited = db.get(Expense, expense.id)
    assert edited.date == dt.date(2026, 2, 10)
    assert edited.amount == Decimal("1500.00")
    assert edited.category == "уборка"
    assert edited.description == "исправлено"


def test_editing_non_last_expense_is_allowed_and_reallocates_payments(db, client):
    """В отличие от платежей, у расходов нет ограничения «только последний»
    — правим самый первый (по дате) из трёх, разнесение платежа
    пересчитывается заново."""
    counterparty = make_counterparty(db)
    e1 = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00"))
    e2 = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 5), amount=Decimal("200.00"))
    e3 = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 10), amount=Decimal("300.00"))
    db.add_all([e1, e2, e3])
    db.add(CounterpartyPayment(counterparty_id=counterparty.id, date=dt.date(2026, 1, 15), amount=Decimal("150.00")))
    make_user(db, "board61", "pass12345", role=RoleEnum.BOARD)
    db.commit()

    login(client, "board61", "pass12345")
    resp = client.post(f"/counterparties/{counterparty.id}/expenses/{e1.id}/edit", data={
        "date": e1.date.isoformat(), "amount": "50.00", "category": "",
    })
    assert resp.status_code == 302
    db.expire_all()

    e1_after = db.get(Expense, e1.id)
    assert e1_after.amount == Decimal("50.00")
    # 150 оплачено -> после уменьшения e1 до 50 полностью гасит e1 (50) и
    # частично e2 (100 из 200), e3 не тронут вовсе — проверяем именно этот пересчёт
    paid_e1 = sum(a.amount for a in e1_after.allocations)
    paid_e2 = sum(a.amount for a in db.get(Expense, e2.id).allocations)
    paid_e3 = sum(a.amount for a in db.get(Expense, e3.id).allocations)
    assert paid_e1 == Decimal("50.00")
    assert paid_e2 == Decimal("100.00")
    assert paid_e3 == Decimal("0")


def test_add_expense_with_two_files_creates_two_documents(db, client):
    """Реальный случай, из-за которого расходу может понадобиться больше
    одного подтверждающего документа: регистрация домена оформляется одним
    УПД, аренда ПО веб-панели для его DNS — другим, а кооператив ведёт это
    одной строкой расхода (см. Expense.documents)."""
    counterparty = make_counterparty(db)
    make_user(db, "board62", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board62", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01", "amount": "1500.00",
        "document_file": [
            (io.BytesIO(b"upd domain"), "upd-domain.pdf"),
            (io.BytesIO(b"upd panel"), "upd-panel.pdf"),
        ],
    })
    assert resp.status_code == 302
    db.expire_all()

    expense = db.query(Expense).filter_by(counterparty_id=counterparty.id).one()
    assert len(expense.documents) == 2
    titles = {d.title for d in expense.documents}
    assert titles == {"upd-domain.pdf", "upd-panel.pdf"}
    # оба документа видны и в общем разделе "Документы" контрагента
    assert set(expense.documents) <= set(counterparty.documents)


def test_edit_expense_keeps_existing_documents_when_no_new_file_uploaded(db, client):
    counterparty = make_counterparty(db)
    make_user(db, "board62b", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board62b", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01", "amount": "500.00",
        "document_file": (io.BytesIO(b"original file"), "invoice.pdf"),
    })
    db.expire_all()
    expense = db.query(Expense).filter_by(counterparty_id=counterparty.id).one()
    original_doc_ids = {d.id for d in expense.documents}
    assert original_doc_ids

    resp = client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-01-01", "amount": "600.00", "category": "",
    })
    assert resp.status_code == 302
    db.expire_all()
    assert {d.id for d in db.get(Expense, expense.id).documents} == original_doc_ids


def test_edit_expense_adds_new_document_without_removing_existing_one(db, client):
    counterparty = make_counterparty(db)
    make_user(db, "board63", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board63", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01", "amount": "500.00",
        "document_file": (io.BytesIO(b"original file"), "invoice.pdf"),
    })
    db.expire_all()
    expense = db.query(Expense).filter_by(counterparty_id=counterparty.id).one()
    original_doc_ids = {d.id for d in expense.documents}

    client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-01-01", "amount": "500.00", "category": "",
        "document_file": (io.BytesIO(b"second upd"), "invoice-2.pdf"),
    })
    db.expire_all()
    new_doc_ids = {d.id for d in db.get(Expense, expense.id).documents}
    assert original_doc_ids < new_doc_ids  # старый документ остался, добавился новый
    assert len(new_doc_ids) == 2


def test_edit_expense_writes_audit_log(db, client):
    counterparty = make_counterparty(db)
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00"))
    db.add(expense)
    make_user(db, "board64", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board64", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-01-02", "amount": "200.00", "category": "",
    })
    entry = db.query(AuditLog).filter_by(action="expense.edit").one()
    assert counterparty.name in entry.summary


def test_member_cannot_edit_expense(db, client):
    counterparty = make_counterparty(db)
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00"))
    db.add(expense)
    make_user(db, "member60", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member60", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-01-02", "amount": "200.00",
    })
    assert resp.status_code == 302
    db.expire_all()
    assert db.get(Expense, expense.id).amount == Decimal("100.00")


def test_edit_expense_404_for_expense_of_another_counterparty(db, client):
    counterparty = make_counterparty(db, name="ООО Ромашка")
    other = make_counterparty(db, name="ООО Василёк")
    expense = Expense(counterparty_id=other.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00"))
    db.add(expense)
    make_user(db, "board65", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board65", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/expenses/{expense.id}/edit", data={
        "date": "2026-01-02", "amount": "200.00",
    })
    assert resp.status_code == 404


def test_detail_page_shows_edit_button_for_expense(db, client):
    counterparty = make_counterparty(db)
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00"))
    db.add(expense)
    make_user(db, "board66", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board66", "pass12345")

    resp = client.get(f"/counterparties/{counterparty.id}")
    body = resp.get_data(as_text=True)
    assert f'data-bs-target="#editExpenseModal{expense.id}"' in body
    assert f'/counterparties/{counterparty.id}/expenses/{expense.id}/edit' in body


def test_expense_document_defaults_to_utd_type(db, client):
    """Документы, прикреплённые к расходу через форму (см.
    _save_expense_documents), обычно и есть УПД/счета-фактуры от
    контрагента — не общий "Акт" (см. DocumentType.UTD)."""
    counterparty = make_counterparty(db)
    make_user(db, "board67", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board67", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01", "amount": "500.00",
        "document_file": (io.BytesIO(b"upd"), "upd.pdf"),
    })
    db.expire_all()
    expense = db.query(Expense).filter_by(counterparty_id=counterparty.id).one()
    assert expense.documents[0].doc_type == DocumentType.UTD

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert "УПД / счёт-фактура" in resp.get_data(as_text=True)
