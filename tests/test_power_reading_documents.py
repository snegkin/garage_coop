import datetime as dt
import io
from decimal import Decimal

from app import database
from app.models import (
    Counterparty, DocumentType, ElectricityTariff, ElectricityTariffKind, Expense, MasterMeterReading, RoleEnum,
)
from tests.conftest import login, make_user


def _setup(db, client):
    counterparty = Counterparty(name="ТНС Энерго")
    tariff = ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("5"), effective_date=dt.date(2025, 1, 1))
    db.add_all([counterparty, tariff])
    db.flush()
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 9, 1), amount=Decimal("500.00"))
    db.add(expense)
    db.flush()
    older = MasterMeterReading(year=2026, month=9, reading_date=dt.date(2026, 9, 1), reading=Decimal("100"),
                               tariff_id=tariff.id, expense_id=expense.id)
    latest = MasterMeterReading(year=2026, month=10, reading_date=dt.date(2026, 10, 1), reading=Decimal("200"),
                                tariff_id=tariff.id)
    db.add_all([older, latest])
    make_user(db, "board_rd", "pw12345678", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_rd", "pw12345678")
    return older, latest, expense


def test_attach_document_to_older_reading(app, db, client):
    older, latest, expense = _setup(db, client)
    resp = client.post(
        f"/power/readings/{older.id}/document",
        data={"document_file": (io.BytesIO(b"%PDF-1.4"), "schet.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    database.db_session.expire_all()
    reading = database.db_session.get(MasterMeterReading, older.id)
    assert reading.document is not None
    assert reading.document.expense_id == expense.id
    assert "9.2026" in reading.document.title
    # Счёт поставщика — только правлению и у контрагента, как прочие
    # документы по расчётам с ним.
    assert reading.document.is_internal is True
    assert reading.document.is_public is False
    assert reading.document.counterparty_id == expense.counterparty_id
    assert reading.document.doc_type == DocumentType.INVOICE

    # Повторная загрузка заменяет документ у показания.
    first_doc_id = reading.document_id
    client.post(
        f"/power/readings/{older.id}/document",
        data={"document_file": (io.BytesIO(b"%PDF-1.4"), "schet2.pdf"), "document_title": "Акт"},
        content_type="multipart/form-data",
    )
    database.db_session.expire_all()
    reading = database.db_session.get(MasterMeterReading, older.id)
    assert reading.document_id != first_doc_id
    assert reading.document.title == "Акт"


def test_attach_document_rejects_bad_extension(app, db, client):
    older, _latest, _expense = _setup(db, client)
    client.post(
        f"/power/readings/{older.id}/document",
        data={"document_file": (io.BytesIO(b"<script>"), "x.html")},
        content_type="multipart/form-data",
    )
    database.db_session.expire_all()
    assert database.db_session.get(MasterMeterReading, older.id).document_id is None


def test_edit_latest_reading_with_document(app, db, client):
    _older, latest, _expense = _setup(db, client)
    resp = client.post(
        f"/power/readings/{latest.id}/edit",
        data={"year": "2026", "month": "10", "reading": "200", "line_loss": "", "comment": "",
              "document_file": (io.BytesIO(b"%PDF-1.4"), "schet.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    database.db_session.expire_all()
    reading = database.db_session.get(MasterMeterReading, latest.id)
    assert reading.document is not None
    assert reading.document.expense_id == reading.expense_id


def test_view_shows_attach_buttons(app, db, client):
    older, latest, _expense = _setup(db, client)
    html = client.get("/power/").get_data(as_text=True)
    assert f"/power/readings/{older.id}/document" in html
    assert f"/power/readings/{latest.id}/document" in html
