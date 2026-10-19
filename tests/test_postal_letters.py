"""
Заказные письма через Почту России (app/postal_letters.py, /postal-letters/) —
задача минимум: формирование реестра .xls + PDF-писем для РУЧНОЙ загрузки в
otpravka.pochta.ru, без прямой интеграции с API Почты (см. докстринг
app/postal_letters.py). Первая версия — только уведомления о задолженности,
тот же список должников, что и у app.legal_docs.debt_notice.
"""
import datetime as dt
import io
import os
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
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=f"1{person.id:03d}")
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
    assert client.get("/postal-letters/").status_code == 200
    resp = client.get("/postal-letters/new")  # выбор должников — общая страница «Должники»
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/legal-docs/debtors")


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
    assert dispatch.reg_number == f"{dt.date.today().year}-1"
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


def test_export_ignores_sent_dispatches(db, client):
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


# ---------------------------------------------------------------------------
# Номер письма, правка текста, повторная выгрузка, возврат и удаление
# ---------------------------------------------------------------------------

def _new_letters(db, client, *persons):
    client.post("/postal-letters/new", data={"person_id": [str(p.id) for p in persons]})
    return {d.person_id: d for d in db.query(PostalDispatch).all()}


def _upload_path(app, document):
    return os.path.join(app.config["UPLOAD_FOLDER"], document.file_path)


def test_reg_numbers_are_year_and_sequence_and_survive_deletion(app, db, client):
    _make_coop(db)
    _board_login(db, client)
    first, second, third = _make_debtor(db, "Первов П. П."), _make_debtor(db, "Вторых В. В."), _make_debtor(db, "Третьяков Т. Т.")
    db.add(PostalDispatch(person_id=third.id, reg_number=f"{dt.date.today().year - 1}-7", title="Прошлогоднее",
                          status=PostalDispatchStatus.DELIVERED))
    db.commit()
    year = dt.date.today().year

    letters = _new_letters(db, client, first, second)
    assert sorted(d.reg_number for pid, d in letters.items() if pid in (first.id, second.id)) == [f"{year}-1", f"{year}-2"]

    # удалили последний — номер не переиспользуется
    client.post("/postal-letters/delete", data={"dispatch_id": [str(letters[second.id].id)]})
    client.post("/postal-letters/new", data={"person_id": [str(third.id)]})
    newest = db.query(PostalDispatch).filter_by(person_id=third.id, status=PostalDispatchStatus.DRAFT).one()
    assert newest.reg_number == f"{year}-3"  # не {year}-2: удалённое письмо могло уйти в ЛК Почты под своим номером


def test_edit_draft_text_rebuilds_pdf(app, db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    dispatch = _new_letters(db, client, person)[person.id]
    old_file = _upload_path(app, dispatch.document)
    assert os.path.exists(old_file)

    page = client.get(f"/postal-letters/{dispatch.id}/edit").get_data(as_text=True)
    assert "Настоящим уведомляем Вас" in page  # стандартный текст подставлен в форму

    resp = client.post(f"/postal-letters/{dispatch.id}/edit", data={
        "intro_text": "Свой вводный абзац.\n\nВторой абзац.", "closing_text": "Просим оплатить до конца месяца.",
    })
    assert resp.status_code == 302
    db.refresh(dispatch)
    assert dispatch.intro_text == "Свой вводный абзац.\n\nВторой абзац."
    assert dispatch.closing_text == "Просим оплатить до конца месяца."
    assert not os.path.exists(old_file)  # PDF пересобран, старый файл удалён
    assert os.path.exists(_upload_path(app, dispatch.document))

    client.post(f"/postal-letters/{dispatch.id}/edit", data={"reset": "1"})
    db.refresh(dispatch)
    assert dispatch.intro_text is None and dispatch.closing_text is None


def test_custom_text_rendered_in_letter_with_paragraphs_and_escaping(app):
    from flask import render_template
    with app.test_request_context():
        html = render_template("legal_docs/debt_notice_pdf.html", docs=[{
            "person": type("P", (), {"full_name": "Иванов И. И.", "residence_address": None, "registration_address": None,
                                     "passport_series": None, "phones": [], "email": None, "telegram": None, "vk": None,
                                     "max_messenger": None})(),
            "summary": type("S", (), {"penalty_rows": [], "grand_total": 0})(), "years": [],
            "intro_text": "Первый <b>абзац</b>\nс переносом.\n\nВторой абзац.", "closing_text": None,
        }], coop=None, chairman=None, today=dt.date(2026, 10, 1))
    assert '<p class="legal-p">Первый &lt;b&gt;абзац&lt;/b&gt;<br>с переносом.</p>' in html
    assert '<p class="legal-p">Второй абзац.</p>' in html
    assert "Просим погасить указанную задолженность" in html  # заключительный — стандартный


def test_exported_can_be_downloaded_again_reverted_and_deleted(app, db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    dispatch = _new_letters(db, client, person)[person.id]
    client.post("/postal-letters/export", data={"dispatch_id": [str(dispatch.id)]})
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.EXPORTED

    # пакет потерялся при подписи — скачать ещё раз
    resp = client.post("/postal-letters/export", data={"dispatch_id": [str(dispatch.id)]})
    assert resp.mimetype == "application/zip"
    assert f"{dispatch.reg_number}.pdf" in zipfile.ZipFile(io.BytesIO(resp.data)).namelist()

    # выгруженное не правится, пока не вернули в черновики
    resp = client.get(f"/postal-letters/{dispatch.id}/edit")
    assert resp.status_code == 302
    client.post("/postal-letters/to-draft", data={"dispatch_id": [str(dispatch.id)]})
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.DRAFT and dispatch.exported_at is None
    assert client.get(f"/postal-letters/{dispatch.id}/edit").status_code == 200

    pdf_path = _upload_path(app, dispatch.document)
    document_id = dispatch.document_id
    client.post("/postal-letters/delete", data={"dispatch_id": [str(dispatch.id)]})
    from app.models import Document
    assert db.get(PostalDispatch, dispatch.id) is None
    assert db.get(Document, document_id) is None
    assert not os.path.exists(pdf_path)


def test_sent_letters_cannot_be_deleted_or_reverted(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    dispatch = PostalDispatch(person_id=person.id, reg_number="2026-50", title="Письмо", status=PostalDispatchStatus.SENT)
    db.add(dispatch)
    db.commit()

    client.post("/postal-letters/delete", data={"dispatch_id": [str(dispatch.id)]})
    client.post("/postal-letters/to-draft", data={"dispatch_id": [str(dispatch.id)]})
    db.refresh(dispatch)
    assert dispatch.status == PostalDispatchStatus.SENT


def test_list_links_names_and_row_forms_are_not_nested(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = _make_debtor(db)
    db.commit()
    db.add(PostalDispatch(person_id=person.id, reg_number="2026-60", title="Письмо", status=PostalDispatchStatus.EXPORTED))
    db.commit()

    html = client.get("/postal-letters/").get_data(as_text=True)
    assert f'href="/persons/{person.id}"' in html
    assert 'id="postalSelectAll"' in html and 'id="postalClear"' in html
    # групповая форма пустая (чекбоксы привязаны через form=), строки — свои формы
    bulk = html[html.index('id="postalBulkForm"'):]
    assert bulk.index("</form>") < bulk.index("mark-sent")
    assert 'form="postalBulkForm"' in html
