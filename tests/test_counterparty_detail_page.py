"""
Тесты на карточку контрагента (`/counterparties/<id>`): фильтр+пагинация у
таблиц расходов/платежей/актов сверки (общая разметка data-page-size /
data-table-filter-for / data-pager-for, см. app/templates/base.html) и
отдельный раздел «Документы», показывающий документы, привязанные к
контрагенту (Document.counterparty_id, см. tests/test_document_counterparty.py
для самой привязки).
"""
import io

from app.models import RoleEnum, Counterparty

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
