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

    # На /cooperative/ поле "Контрагент" в форме есть всегда (в отличие от
    # формы на карточке контрагента, см. app/cooperative.py:edit_document) —
    # выбор "— не указан —" отправляет пустую строку, а не пропускает ключ.
    resp = client.post(f"/cooperative/documents/{doc.id}/edit", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Акт",
        "counterparty_id": "",
    })
    assert resp.status_code == 302
    db.refresh(doc)
    assert doc.counterparty_id is None


def test_edit_document_without_counterparty_field_keeps_existing_link(db, client):
    """Форма на карточке контрагента не содержит поле counterparty_id вовсе
    (в отличие от формы на /cooperative/, см. предыдущий тест) — отсутствие
    ключа должно оставлять привязку как есть, а не отвязывать документ."""
    _make_board(db)
    counterparty = _make_counterparty(db)
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт", counterparty_id=counterparty.id)
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/cooperative/documents/{doc.id}/edit", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Акт",
    })
    assert resp.status_code == 302
    db.refresh(doc)
    assert doc.counterparty_id == counterparty.id


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


def test_documents_with_counterparty_are_hidden_from_cooperative_list(db, client):
    """Документ, привязанный к контрагенту, показывается только в его
    карточке (см. tests/test_counterparty_detail_page.py) — не дублируется
    в общем списке на /cooperative/."""
    _make_board(db)
    counterparty = _make_counterparty(db)
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт сверки", counterparty_id=counterparty.id)
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Акт сверки" not in body


def test_documents_without_counterparty_shown_on_cooperative_list(db, client):
    _make_board(db)
    doc = Document(doc_type=DocumentType.CHARTER, date=dt.date(2026, 1, 1), title="Устав кооператива")
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    assert "Устав кооператива" in resp.get_data(as_text=True)
