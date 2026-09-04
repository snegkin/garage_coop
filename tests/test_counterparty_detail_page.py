"""
Тесты на карточку контрагента (`/counterparties/<id>`): фильтр+пагинация у
таблиц расходов/платежей/актов сверки (общая разметка data-page-size /
data-table-filter-for / data-pager-for, см. app/templates/base.html) и
отдельный раздел «Документы», показывающий документы, привязанные к
контрагенту (Document.counterparty_id, см. tests/test_document_counterparty.py
для самой привязки).
"""
import datetime as dt
import io

from app.models import RoleEnum, Counterparty, Document, DocumentType

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


def test_detail_page_has_filter_and_pager_markup_for_all_tables(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01", "amount": "500.00",
    })
    assert resp.status_code == 302

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    for table_id in ("expensesTable", "paymentsTable", "actsTable", "counterpartyDocumentsTable"):
        assert f'id="{table_id}"' in body
        assert f'data-page-size' in body
        assert f'data-pager-for="{table_id}"' in body
        assert f'data-no-results-for="{table_id}"' in body
    # Инпут поиска появляется только когда в таблице есть хотя бы одна строка
    assert 'data-table-filter-for="expensesTable"' in body


def test_detail_page_documents_section_lists_attached_document(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/counterparties/{counterparty.id}/expenses/new", data={
        "date": "2026-01-01",
        "amount": "1000.00",
        "document_file": (io.BytesIO(b"file contents"), "invoice.pdf"),
        "document_title": "Счёт №42",
    })
    assert resp.status_code == 302

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Счёт №42" in body
    assert "counterpartyDocumentsTable" in body


def test_detail_page_documents_section_empty_state(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Документов, связанных с этим контрагентом, пока нет." in body


def test_detail_page_has_add_document_button(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="addDocumentModal"' in body
    assert f'action="/counterparties/{counterparty.id}/documents/new"' in body


def test_add_document_creates_document_linked_to_counterparty(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/counterparties/{counterparty.id}/documents/new", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Договор оказания услуг",
        "file": (io.BytesIO(b"file contents"), "contract.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/counterparties/{counterparty.id}")

    doc = db.query(Document).filter_by(title="Договор оказания услуг").first()
    assert doc is not None
    assert doc.counterparty_id == counterparty.id
    assert doc.file_path is not None
    # Документы контрагента — всегда внутренние, без права выбора в форме.
    assert doc.is_internal is True


def test_add_document_form_has_no_internal_checkbox(db, client):
    """Раз выбора нет (см. предыдущий тест) — сам чекбокс не должен вводить
    в заблуждение видимостью в разметке формы."""
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    assert 'name="is_internal"' not in resp.get_data(as_text=True)


def test_documents_section_has_actions_dropdown_with_edit_and_delete(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/counterparties/{counterparty.id}/documents/new", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Договор оказания услуг",
        "file": (io.BytesIO(b"file contents"), "contract.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    doc = db.query(Document).filter_by(title="Договор оказания услуг").first()

    resp = client.get(f"/counterparties/{counterparty.id}")
    body = resp.get_data(as_text=True)
    assert f'id="editCounterpartyDocumentModal{doc.id}"' in body
    assert f'action="/cooperative/documents/{doc.id}/edit"' in body
    assert f'action="/cooperative/documents/{doc.id}/delete"' in body


def test_edit_document_from_counterparty_page_keeps_counterparty_link(db, client):
    """Форма правки документа на карточке контрагента не содержит поля
    counterparty_id (оно есть только в форме на /cooperative/, см.
    cooperative/_document_fields.html) — отсутствие ключа в POST не должно
    отвязывать документ от контрагента (см. app/cooperative.py:edit_document)."""
    _make_board(db)
    counterparty = _make_counterparty(db)
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт", counterparty_id=counterparty.id)
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/cooperative/documents/{doc.id}/edit", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Акт сверки (правка)",
    })
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/counterparties/{counterparty.id}#documentsSection")
    db.refresh(doc)
    assert doc.title == "Акт сверки (правка)"
    assert doc.counterparty_id == counterparty.id


def test_delete_document_from_counterparty_page_redirects_back(db, client):
    _make_board(db)
    counterparty = _make_counterparty(db)
    doc = Document(doc_type=DocumentType.ACT, date=dt.date(2026, 1, 1), title="Акт", counterparty_id=counterparty.id)
    db.add(doc)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/cooperative/documents/{doc.id}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/counterparties/{counterparty.id}#documentsSection")
    assert db.query(Document).filter_by(id=doc.id).first() is None


def test_only_board_can_add_document_to_counterparty(db, client):
    person = make_person(db, full_name="Member One")
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    counterparty = _make_counterparty(db)
    db.commit()

    login(client, "member1", "pass1234")
    resp = client.post(f"/counterparties/{counterparty.id}/documents/new", data={
        "doc_type": DocumentType.ACT.value,
        "date": "2026-01-01",
        "title": "Sneaky doc",
    })
    assert resp.status_code == 302
    assert db.query(Document).filter_by(title="Sneaky doc").first() is None


def test_detail_page_shows_each_comma_separated_phone_as_own_link(db, client):
    """Counterparty.phone — одно текстовое поле, несколько номеров вводят
    через запятую (нет отдельной таблицы, как Person.phones) — раньше все
    цифры слипались в один нерабочий tel: ("супертелефон"), см.
    contact_format.phone_link."""
    _make_board(db)
    counterparty = _make_counterparty(db)
    counterparty.phone = "+7 911 111-11-11, +7 922 222-22-22"
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.get(f"/counterparties/{counterparty.id}")
    body = resp.get_data(as_text=True)
    assert 'href="tel:+79111111111"' in body
    assert 'href="tel:+79222222222"' in body
    assert 'href="tel:+79111111111+79222222222"' not in body
