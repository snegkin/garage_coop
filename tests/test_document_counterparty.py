"""
Тесты на связь документа с контрагентом (Document.counterparty_id) — как
на ручное указание контрагента в форме документа (app/cooperative.py:
create_document/edit_document), так и на автоматическую привязку при
прикреплении файла к расходу/платежу/акту сверки контрагента
(app/counterparties.py: _save_document).
"""
import datetime as dt
import io

from app.models import RoleEnum, Document, DocumentType, Counterparty

from tests.conftest import make_person, make_user, login


def _make_board(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def _make_counterparty(db, name="ООО Ромашка"):
    c = Counterparty(name=name)
    db.add(c)
    db.flush()
    return c


def test_create_document_with_counterparty_links_it(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post("/cooperative/documents/new", data={
        "doc_type": DocumentType.INVOICE.value,
        "date": "2026-01-01",
        "title": "Счёт от Ромашки",
        "counterparty_id": str(counterparty.id),
    })
    assert resp.status_code == 302
    doc = db.query(Document).filter_by(title="Счёт от Ромашки").first()
    assert doc is not None
    assert doc.counterparty_id == counterparty.id


def test_create_document_without_counterparty_leaves_it_unset(db, client):
    _make_board(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post("/cooperative/documents/new", data={
        "doc_type": DocumentType.OTHER.value,
        "date": "2026-01-01",
        "title": "Документ без контрагента",
    })
    assert resp.status_code == 302
    doc = db.query(Document).filter_by(title="Документ без контрагента").first()
    assert doc is not None
    assert doc.counterparty_id is None


def test_edit_document_can_change_and_clear_counterparty(db, client):
    _make_board(db)
    c1 = _make_counterparty(db, name="Контрагент 1")
    c2 = _make_counterparty(db, name="Контрагент 2")
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт", counterparty_id=c1.id)
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/cooperative/documents/{doc.id}/edit", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Акт",
        "counterparty_id": str(c2.id),
    })
    assert resp.status_code == 302
    db.refresh(doc)
    assert doc.counterparty_id == c2.id

    resp = client.post(f"/cooperative/documents/{doc.id}/edit", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Акт",
    })
    assert resp.status_code == 302
    db.refresh(doc)
    assert doc.counterparty_id is None


def test_expense_attachment_auto_links_document_to_counterparty(app, db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01",
        "amount": "1000.00",
        "document_file": (io.BytesIO(b"file contents"), "invoice.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    doc = db.query(Document).order_by(Document.id.desc()).first()
    assert doc is not None
    assert doc.counterparty_id == counterparty.id
    # Документы, прикреплённые через расчёты с контрагентом, — всегда
    # внутренние (не для рядовых членов), см. _save_document.
    assert doc.is_internal is True


def test_documents_list_shows_counterparty_filter_for_board(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт сверки", counterparty_id=counterparty.id)
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-select-filter-for="documentsTable"' in body
    assert counterparty.name in body
    assert f'data-counterparty-id="{counterparty.id}"' in body


def test_documents_list_hides_counterparty_filter_for_member(db, client):
    person = make_person(db, full_name="Member One")
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    counterparty = _make_counterparty(db)
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт сверки", is_internal=False, counterparty_id=counterparty.id)
    db.add(doc)
    db.commit()

    login(client, "member1", "pass1234")
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Весь блок с select обёрнут в {% if is_board() and all_counterparties %}
    # (см. cooperative/view.html) — для остальных ролей его не должно быть
    # в разметке вовсе, не только "визуально скрыт".
    assert 'data-select-filter-for="documentsTable"' not in body
