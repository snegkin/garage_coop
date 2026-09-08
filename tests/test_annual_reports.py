import datetime as dt
from io import BytesIO

from tests.conftest import make_user, login
from app.models import (
    RoleEnum, GeneralMeeting, AnnualReport, FeeRate, FeeType, Document, DocumentType,
)


def _make_meeting(db, is_annual_report_meeting=True, date=dt.date(2026, 3, 1)):
    meeting = GeneralMeeting(date=date, is_annual_report_meeting=is_annual_report_meeting)
    db.add(meeting)
    db.flush()
    return meeting


def _make_fee_type(db, code="membership", name="Членский взнос"):
    fee_type = FeeType(code=code, name=name, type_code="01", is_penalty=False)
    db.add(fee_type)
    db.flush()
    return fee_type


def test_member_cannot_create_report(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    meeting = _make_meeting(db)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post(f"/annual-reports/new/{meeting.id}", data={"year": "2026"})
    assert resp.status_code == 302
    assert db.query(AnnualReport).count() == 0


def test_board_cannot_create_report(db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    meeting = _make_meeting(db)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.post(f"/annual-reports/new/{meeting.id}", data={"year": "2026"})
    assert resp.status_code == 302
    assert db.query(AnnualReport).count() == 0


def test_create_requires_annual_report_meeting(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db, is_annual_report_meeting=False)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.get(f"/annual-reports/new/{meeting.id}")
    assert resp.status_code == 404


def test_chairman_creates_report_with_documents_and_fee_rates(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db)
    fee_type = _make_fee_type(db)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.post(f"/annual-reports/new/{meeting.id}", data={
        "year": "2026",
        "budget_total": "150000",
        "budget_approved": "on",
        "comment": "Смета одобрена единогласно",
        "spending_report_file": (BytesIO(b"fake pdf"), "spending.pdf"),
        "accounting_report_file": (BytesIO(b"fake pdf"), "accounting.pdf"),
        f"rate_per_sqm_{fee_type.id}": "120.50",
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    report = db.query(AnnualReport).one()
    assert report.meeting_id == meeting.id
    assert report.year == 2026
    assert str(report.budget_total) == "150000.00"
    assert report.budget_approved is True

    spending_doc = db.get(Document, report.spending_report_document_id)
    accounting_doc = db.get(Document, report.accounting_report_document_id)
    assert spending_doc.doc_type == DocumentType.REPORT
    assert accounting_doc.doc_type == DocumentType.REPORT

    rates = db.query(FeeRate).filter_by(annual_report_id=report.id).all()
    assert len(rates) == 1
    assert rates[0].fee_type_id == fee_type.id
    assert str(rates[0].rate_per_sqm) == "120.50"


def test_create_twice_redirects_to_existing_report_edit(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db)
    db.commit()
    login(client, "chair1", "pass12345")

    client.post(f"/annual-reports/new/{meeting.id}", data={"year": "2026"})
    report = db.query(AnnualReport).one()

    resp = client.get(f"/annual-reports/new/{meeting.id}")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/annual-reports/{report.id}/edit")
    assert db.query(AnnualReport).count() == 1


def test_edit_replaces_document_and_clears_fee_rate(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db)
    fee_type = _make_fee_type(db)
    db.commit()
    login(client, "chair1", "pass12345")

    client.post(f"/annual-reports/new/{meeting.id}", data={
        "year": "2026",
        "spending_report_file": (BytesIO(b"v1"), "v1.pdf"),
        f"rate_per_sqm_{fee_type.id}": "100",
    }, content_type="multipart/form-data")
    report = db.query(AnnualReport).one()
    old_doc_id = report.spending_report_document_id
    assert db.query(FeeRate).filter_by(annual_report_id=report.id).count() == 1

    resp = client.post(f"/annual-reports/{report.id}/edit", data={
        "year": "2026",
        "spending_report_file": (BytesIO(b"v2"), "v2.pdf"),
        f"rate_per_sqm_{fee_type.id}": "",
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    db.expire_all()
    report = db.query(AnnualReport).one()
    assert report.spending_report_document_id != old_doc_id
    new_doc = db.get(Document, report.spending_report_document_id)
    assert new_doc.file_name == "v2.pdf"
    assert db.query(FeeRate).filter_by(annual_report_id=report.id).count() == 0


def test_view_accessible_to_plain_member(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db)
    db.commit()
    login(client, "chair1", "pass12345")
    client.post(f"/annual-reports/new/{meeting.id}", data={"year": "2026", "comment": "Отчёт готов"})
    report = db.query(AnnualReport).one()
    client.get("/auth/logout")

    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get(f"/annual-reports/{report.id}")
    assert resp.status_code == 200
    assert "Отчёт готов" in resp.get_data(as_text=True)


def test_meetings_list_shows_create_then_open_link(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db)
    db.commit()
    login(client, "chair1", "pass12345")

    body = client.get("/meetings/").get_data(as_text=True)
    assert f"/annual-reports/new/{meeting.id}" in body

    client.post(f"/annual-reports/new/{meeting.id}", data={"year": "2026"})
    report = db.query(AnnualReport).one()

    body = client.get("/meetings/").get_data(as_text=True)
    assert f"/annual-reports/{report.id}" in body
    assert f"/annual-reports/new/{meeting.id}" not in body


def test_dashboard_links_to_last_report(db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    meeting = _make_meeting(db)
    db.commit()
    login(client, "chair1", "pass12345")
    client.post(f"/annual-reports/new/{meeting.id}", data={"year": "2026"})
    report = db.query(AnnualReport).one()

    body = client.get("/dashboard").get_data(as_text=True)
    assert f"/annual-reports/{report.id}" in body
