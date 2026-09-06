"""
Раздел «Делопроизводство» (app/legal_docs.py, /legal-docs/) — взыскание
задолженности через суд: уведомление о задолженности, оплата госпошлины,
исковое заявление, справочник судебных участков (CourtSection).
"""
import datetime as dt
from decimal import Decimal

from app.legal_docs import list_debtor_persons, resolve_court_section, suggest_state_duty
from app.models import (
    Cooperative, RoleEnum, CourtSection, FeeType, MemberAccount, Charge, Payment,
    Garage, GarageOwnership, PersonalAccount, KeyRate,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_coop(db, **kwargs):
    coop = Cooperative(
        full_name="Тестовый гаражный кооператив", short_name="ТГК",
        inn="1234567890", kpp="123456789", ogrn="1234567890123",
        legal_address="г. Тестоград, ул. Гаражная, д. 1",
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


# ---------------------------------------------------------------------------
# Права доступа
# ---------------------------------------------------------------------------

def test_plain_member_cannot_access_any_tool(db, client):
    person = make_person(db, full_name="Рядовой Член Членович")
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member1", "pass1234")

    for url in ("/legal-docs/debt-notice", "/legal-docs/state-duty", "/legal-docs/lawsuit"):
        resp = client.get(url)
        assert resp.status_code == 302
        assert "/auth/login" not in resp.headers["Location"]  # залогинен, просто нет прав


def test_board_member_can_access_tool_pickers(db, client):
    _make_coop(db)
    _board_login(db, client)
    for url in ("/legal-docs/debt-notice", "/legal-docs/state-duty", "/legal-docs/lawsuit"):
        resp = client.get(url)
        assert resp.status_code == 200


def test_board_member_cannot_access_court_sections(db, client):
    _make_coop(db)
    _board_login(db, client)
    resp = client.get("/legal-docs/court-sections")
    assert resp.status_code == 302


def test_chairman_can_manage_court_sections(db, client):
    _make_coop(db)
    _board_login(db, client, username="chair1", role=RoleEnum.CHAIRMAN)

    resp = client.get("/legal-docs/court-sections")
    assert resp.status_code == 200

    resp = client.post("/legal-docs/court-sections/new", data={
        "name": "Судебный участок №1", "treasury_payee": "УФК по Тестограду",
    })
    assert resp.status_code == 302
    section = db.query(CourtSection).filter_by(name="Судебный участок №1").one()
    assert section.treasury_payee == "УФК по Тестограду"

    resp = client.post(f"/legal-docs/court-sections/{section.id}/edit", data={
        "name": "Судебный участок №1 (изменён)",
    })
    assert resp.status_code == 302
    db.refresh(section)
    assert section.name == "Судебный участок №1 (изменён)"

    resp = client.post(f"/legal-docs/court-sections/{section.id}/delete")
    assert resp.status_code == 302
    assert db.query(CourtSection).filter_by(id=section.id).first() is None


# ---------------------------------------------------------------------------
# list_debtor_persons
# ---------------------------------------------------------------------------

def test_list_debtor_persons_finds_member_account_debt(db, client):
    _make_coop(db)
    person = make_person(db, full_name="Должников Долг Долгович")
    garage = make_garage(db, number="10")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="5000.00")
    db.commit()

    rows = list_debtor_persons()
    assert len(rows) == 1
    assert rows[0]["person"].id == person.id
    assert rows[0]["debt"] == Decimal("5000.00")


def test_list_debtor_persons_finds_electricity_debt(db, client):
    _make_coop(db)
    person = make_person(db, full_name="Электро Должников")
    garage = make_garage(db, number="11")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="1101"))
    db.add(Charge(garage_id=garage.id, year=2024, amount=Decimal("300.00")))
    db.commit()

    rows = list_debtor_persons()
    assert len(rows) == 1
    assert rows[0]["debt"] == Decimal("300.00")


def test_list_debtor_persons_excludes_person_without_debt(db, client):
    _make_coop(db)
    person = make_person(db, full_name="Без Долгов Долгович")
    garage = make_garage(db, number="12")
    make_ownership(db, garage, person)
    account = _make_debt(db, person, garage, amount="1000.00")
    db.add(Payment(account_id=account.id, date=dt.date(2024, 6, 1), amount=Decimal("1000.00")))
    db.commit()

    rows = list_debtor_persons()
    assert rows == []


# ---------------------------------------------------------------------------
# resolve_court_section — фолбэк
# ---------------------------------------------------------------------------

def test_resolve_court_section_prefers_person_section(db):
    section_person = CourtSection(name="Участок должника")
    section_default = CourtSection(name="Участок кооператива")
    db.add_all([section_person, section_default])
    db.flush()
    coop = _make_coop(db, default_court_section_id=section_default.id)
    person = make_person(db, full_name="Иванов Иван Иванович", court_section_id=section_person.id)
    db.commit()

    assert resolve_court_section(person, coop).id == section_person.id


def test_resolve_court_section_falls_back_to_cooperative_default(db):
    section_default = CourtSection(name="Участок кооператива")
    db.add(section_default)
    db.flush()
    coop = _make_coop(db, default_court_section_id=section_default.id)
    person = make_person(db, full_name="Петров Пётр Петрович")
    db.commit()

    assert resolve_court_section(person, coop).id == section_default.id


def test_resolve_court_section_none_when_neither_set(db):
    coop = _make_coop(db)
    person = make_person(db, full_name="Сидоров Сидор Сидорович")
    db.commit()

    assert resolve_court_section(person, coop) is None


# ---------------------------------------------------------------------------
# 1. Уведомление о задолженности
# ---------------------------------------------------------------------------

def test_debt_notice_print_shows_debtor_and_amount(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Забывалов Забыл Забылович")
    garage = make_garage(db, number="20")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="7500.00")
    db.commit()

    resp = client.post("/legal-docs/debt-notice", data={"person_id": [str(person.id)]})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert person.full_name in body
    assert "7500,00" in body or "7 500,00" in body


def test_debt_notice_requires_at_least_one_person(db, client):
    _make_coop(db)
    _board_login(db, client)
    resp = client.post("/legal-docs/debt-notice", data={})
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# 2. Оплата госпошлины
# ---------------------------------------------------------------------------

def test_suggest_state_duty_brackets():
    assert suggest_state_duty(Decimal("50000")) == Decimal("4000.00")
    assert suggest_state_duty(Decimal("100000")) == Decimal("4000.00")
    assert suggest_state_duty(Decimal("300000")) == Decimal("10000.00")
    assert suggest_state_duty(Decimal("500000")) == Decimal("15000.00")
    assert suggest_state_duty(Decimal("1000000")) == Decimal("25000.00")
    # середина диапазона 100k-300k: 4000 + 3% * (200000-100000) = 7000
    assert suggest_state_duty(Decimal("200000")) == Decimal("7000.00")


def test_state_duty_review_then_print_uses_edited_amount(db, client):
    _make_coop(db)
    _board_login(db, client)
    section = CourtSection(name="Судебный участок №3", treasury_payee="УФК по Тестограду", treasury_kbk="18210803010011000110")
    db.add(section)
    db.flush()
    person = make_person(db, full_name="Госпошлинов Иск Искович", court_section_id=section.id)
    garage = make_garage(db, number="30")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="50000.00")
    db.commit()

    resp = client.post("/legal-docs/state-duty/review", data={"person_id": [str(person.id)]})
    assert resp.status_code == 200
    review_body = resp.get_data(as_text=True)
    assert "4000" in review_body  # автоподсказка для долга ≤100k

    resp = client.post("/legal-docs/state-duty/print", data={
        "person_id": [str(person.id)],
        f"duty_amount_{person.id}": "4500.00",
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "4500,00" in body
    assert "УФК по Тестограду" in body
    assert "18210803010011000110" in body


def test_state_duty_print_without_court_section_shows_warning(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Безучастковый Должник")
    garage = make_garage(db, number="31")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="1000.00")
    db.commit()

    resp = client.post("/legal-docs/state-duty/print", data={
        "person_id": [str(person.id)],
        f"duty_amount_{person.id}": "4000.00",
    })
    assert resp.status_code == 200
    assert "не определ" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 3. Исковое заявление
# ---------------------------------------------------------------------------

def test_lawsuit_draft_contains_legal_basis_and_totals(db, client):
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    _board_login(db, client)
    db.add(KeyRate(rate_percent=Decimal("16.0"), effective_date=dt.date(2023, 1, 1)))
    person = make_person(db, full_name="Ответчиков Иск Искович")
    garage = make_garage(db, number="40")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="12000.00")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/draft", data={"person_id": [str(person.id)]})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "338-ФЗ" in body
    assert "131" in body
    assert "Ответчиков Иск Искович" in body
    assert "12000" in body or "12 000" in body


def test_lawsuit_print_preserves_edited_text_verbatim(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Правкин Черновик Черновикович")
    db.commit()

    edited_text = "ОТРЕДАКТИРОВАННЫЙ ПРАВЛЕНИЕМ ТЕКСТ ИСКА, сумма 99999 руб."
    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": edited_text,
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert edited_text in body


def test_lawsuit_requires_at_least_one_person(db, client):
    _make_coop(db)
    _board_login(db, client)
    resp = client.post("/legal-docs/lawsuit/draft", data={})
    assert resp.status_code == 302
