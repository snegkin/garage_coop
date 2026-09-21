"""
Заказные письма через Почту России (app/postal_letters.py, /postal-letters/) —
задача минимум: формирование реестра .xls + PDF-писем для РУЧНОЙ загрузки в
otpravka.pochta.ru, без прямой интеграции с API Почты (см. докстринг
app/postal_letters.py). Первая версия — только уведомления о задолженности,
тот же список должников, что и у app.legal_docs.debt_notice.
"""
import io
import zipfile
from decimal import Decimal

import xlrd

from app.postal_letters import build_registry_workbook
from app.models import (
    Cooperative, RoleEnum, FeeType, MemberAccount, Charge,
    PostalDispatch, PostalDispatchStatus,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_coop(db, **kwargs):
    coop = Cooperative(
        full_name="Тестовый гаражный кооператив", short_name="ТГК",
        inn="1234567890", kpp="123456789", ogrn="1234567890123",
        legal_address="г. Тестоград, ул. Гаражная, д. 1",
        postal_address="123456, г. Тестоград, ул. Почтовая, д. 2",
        **kwargs,
    )
    db.add(coop)
    db.flush()
    return coop


def _make_debt(db, person, garage, amount="10000.00", fee_code="membership"):
    fee_type = db.query(FeeType).filter_by(code=fee_code).first()
    if fee_type is None:
        fee_type = FeeType(code=fee_code, name="Членский взнос")
        db.add(fee_type)
        db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="1001")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2024, amount=Decimal(amount)))
    db.flush()
    return account


def _board_login(db, client, username="board1", role=RoleEnum.BOARD):
    person = make_person(db, full_name="Правленцев Иван Иванович")
    make_user(db, username, "pass1234", role=role, person=person)
    db.commit()
    login(client, username, "pass1234")
    return person


def _make_debtor(db, full_name="Должников Долг Долгович"):
    person = make_person(db, full_name=full_name, residence_address="600000, г. Тольятти, ул. Гаражная, д. 1, кв. 2")
    garage = make_garage(db, number=str(person.id))
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="5000.00")
    return person


# ---------------------------------------------------------------------------
# Права доступа
# ---------------------------------------------------------------------------

def test_plain_member_cannot_access(db, client):
    person = make_person(db, full_name="Рядовой Член Членович")
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member1", "pass1234")

    for url in ("/postal-letters/", "/postal-letters/new"):
        resp = client.get(url)
        assert resp.status_code == 302
        assert "/auth/login" not in resp.headers["Location"]  # залогинен, просто нет прав


def test_board_member_can_access(db, client):
    _make_coop(db)
    _board_login(db, client)
    for url in ("/postal-letters/", "/postal-letters/new"):
        resp = client.get(url)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# build_registry_workbook — формат реестра (temp/Инструкция.docx)
# ---------------------------------------------------------------------------

def test_build_registry_workbook_columns(db):
    coop = _make_coop(db)
    person = _make_debtor(db)
    db.commit()

    dispatch = PostalDispatch(
        person_id=person.id, reg_number="GSK-1", title="Уведомление о задолженности — Должников",
        status=PostalDispatchStatus.DRAFT,
    )
    db.add(dispatch)
    db.flush()

    xls_bytes = build_registry_workbook([dispatch], coop)
    book = xlrd.open_workbook(file_contents=xls_bytes)
    sheet = book.sheet_by_index(0)

    header = sheet.row_values(0)
    assert header == [
        "FILE_NAME", "ADDRESSLINE_TO", "RECIPIENT_TYPE", "RECIPIENT",
        "LETTER_REG_NUMBER", "LETTER_TITLE", "MAILCATEGORY",
        "ADDRESSLINE_RETURN", "NOTIFICATIONTYPE", "NO_RETURN",
    ]

    row = sheet.row_values(1)
    assert row[0] == "GSK-1.pdf"
    assert row[1] == person.residence_address
    assert row[2] == 0  # физлицо
    assert row[3] == person.full_name
    assert row[4] == "GSK-1"
    assert row[5] == dispatch.title
    assert row[6] == 1  # заказное
    assert row[7] == coop.postal_address
    assert row[8] == "E"  # электронное уведомление о вручении


def test_build_registry_workbook_falls_back_to_registration_address(db):
    coop = _make_coop(db)
    person = make_person(db, full_name="Безместов Иван Иванович", registration_address="г. Тестоград, ул. Прописочная, д. 5")
    db.commit()
    dispatch = PostalDispatch(person_id=person.id, reg_number="GSK-2", title="Письмо", status=PostalDispatchStatus.DRAFT)
    db.add(dispatch)
    db.flush()

    xls_bytes = build_registry_workbook([dispatch], coop)
    book = xlrd.open_workbook(file_contents=xls_bytes)
    row = book.sheet_by_index(0).row_values(1)
    assert row[1] == person.registration_address


# ---------------------------------------------------------------------------
# Полный сценарий: создание, выгрузка, смена статусов
# ---------------------------------------------------------------------------

def test_new_creates_draft_dispatch_with_document(db, client):
    _make_coop(db)
    board = _board_login(db, client)
    person = _make_debtor(db)
    db.commit()

    resp = client.post("/postal-letters/new", data={"person_id": [str(person.id)]})
    assert resp.status_code == 302

    dispatch = db.query(PostalDispatch).filter_by(person_id=person.id).one()
    assert dispatch.status == PostalDispatchStatus.DRAFT
    assert dispatch.reg_number == f"ГСК-{dispatch.id}"
    assert dispatch.created_by_user_id == board.id or dispatch.created_by_user_id is not None
    assert dispatch.document_id is not None
    assert dispatch.document.file_path is not None


def test_export_marks_exported_and_returns_zip(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()

    client.post("/postal-letters/new", data={"person_id": [str(person.id)]})
    dispatch = db.query(PostalDispatch).filter_by(person_id=person.id).one()
    assert dispatch.status == PostalDispatchStatus.DRAFT

    resp = client.post("/postal-letters/export", data={"dispatch_id": [str(dispatch.id)]})
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert "reestr.xls" in names
    assert f"{dispatch.reg_number}.pdf" in names

    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.EXPORTED
    assert dispatch.exported_at is not None


def test_export_ignores_non_draft_dispatches(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    dispatch = PostalDispatch(person_id=person.id, reg_number="GSK-3", title="Письмо", status=PostalDispatchStatus.SENT)
    db.add(dispatch)
    db.commit()

    resp = client.post("/postal-letters/export", data={"dispatch_id": [str(dispatch.id)]})
    assert resp.status_code == 302  # ничего не выгружено, редирект с сообщением об ошибке
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.SENT  # статус не тронут


def test_status_transitions(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    dispatch = PostalDispatch(person_id=person.id, reg_number="GSK-4", title="Письмо", status=PostalDispatchStatus.EXPORTED)
    db.add(dispatch)
    db.commit()

    resp = client.post(f"/postal-letters/{dispatch.id}/mark-sent")
    assert resp.status_code == 302
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.SENT
    assert dispatch.sent_at is not None

    resp = client.post(f"/postal-letters/{dispatch.id}/mark-delivered", data={"tracking_number": "80099999999999"})
    assert resp.status_code == 302
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.DELIVERED
    assert dispatch.tracking_number == "80099999999999"
    assert dispatch.delivered_at is not None


def test_mark_failed_stores_comment(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    dispatch = PostalDispatch(person_id=person.id, reg_number="GSK-5", title="Письмо", status=PostalDispatchStatus.EXPORTED)
    db.add(dispatch)
    db.commit()

    resp = client.post(f"/postal-letters/{dispatch.id}/mark-failed", data={"comment": "Некорректный адрес"})
    assert resp.status_code == 302
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.FAILED
    assert dispatch.comment == "Некорректный адрес"
