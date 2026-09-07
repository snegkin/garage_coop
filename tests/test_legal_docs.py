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


def test_debt_notice_print_hides_board_chat_widget(db, client):
    """Плавающий чат правления мешает превью формального документа —
    на странице печати его быть не должно (см. hide_chat_widgets)."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Тихонов Тихон Тихонович")
    garage = make_garage(db, number="21")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="1000.00")
    db.commit()

    resp = client.post("/legal-docs/debt-notice", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert 'id="boardChatWidget"' not in body


def test_debt_notice_print_uses_portrait_orientation(db, client):
    """Обычная деловая переписка — книжная ориентация (не альбомная)."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Портретов Портрет Портретович")
    garage = make_garage(db, number="22")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="1000.00")
    db.commit()

    resp = client.post("/legal-docs/debt-notice", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert "A4 landscape" not in body


def test_debt_notice_shows_yearly_breakdown_not_lifetime_sum(db, client):
    """Вместо суммирования по счёту за всё время — разбивка по годам:
    видно отдельно, что начислено/оплачено в 2023-м и что в 2024-м, а
    внизу — итог и сумма к погашению."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Погодов Год Годович")
    garage = make_garage(db, number="23")
    make_ownership(db, garage, person)
    account = _make_debt(db, person, garage, amount="5000.00")
    db.add(Charge(account_id=account.id, year=2023, amount=Decimal("3000.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2023, 6, 1), amount=Decimal("1000.00")))
    db.commit()

    resp = client.post("/legal-docs/debt-notice", data={"person_id": [str(person.id)]})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "2023" in body
    assert "2024" in body
    assert "3000,00" in body
    assert "5000,00" in body
    assert "1000,00" in body
    assert "Сумма к погашению" in body
    assert "7000,00" in body  # 5000 (2024) + 3000 (2023) - 1000 (оплачено) = 7000 к погашению


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


def test_state_duty_review_shows_writ_proceeding_toggle(db, client):
    """Чек-бокс «приказное производство» на странице проверки сумм —
    JS на клиенте пересчитывает 100%/50% от суммы, зашитой в data-full-duty
    (см. state_duty_review.html); здесь только проверяем, что нужная
    разметка/данные для этого расчёта присутствуют на странице."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Приказников Приказ Приказович")
    garage = make_garage(db, number="32")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="50000.00")
    db.commit()

    resp = client.post("/legal-docs/state-duty/review", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert f'data-full-duty="4000.00"' in body
    assert "js-writ-toggle" in body
    assert "50% (судебный приказ)" in body


def test_state_duty_print_accepts_halved_writ_proceeding_amount(db, client):
    """Итоговая сумма квитанции остаётся тем, что реально отправлено формой
    (в т.ч. пересчитанное чек-боксом на клиенте значение 50%) — сервер не
    пересчитывает её заново."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Половинкин Полу Половинкин")
    garage = make_garage(db, number="33")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="50000.00")
    db.commit()

    resp = client.post("/legal-docs/state-duty/print", data={
        "person_id": [str(person.id)],
        f"duty_amount_{person.id}": "2000.00",  # 50% от 4000 — как посчитал бы чек-бокс
    })
    assert resp.status_code == 200
    assert "2000,00" in resp.get_data(as_text=True)


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


def test_state_duty_print_hides_board_chat_widget(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Тихонов Госпошлинов")
    garage = make_garage(db, number="34")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="1000.00")
    db.commit()

    resp = client.post("/legal-docs/state-duty/print", data={
        "person_id": [str(person.id)],
        f"duty_amount_{person.id}": "4000.00",
    })
    assert 'id="boardChatWidget"' not in resp.get_data(as_text=True)


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


def test_legal_paragraphs_are_justified_and_have_gap_after_title(db, client):
    """Выравнивание по ширине страницы (как в деловой переписке) и
    зазор в одну строку между заголовком/подзаголовком и текстом — общий
    для уведомления и искового заявления стиль (см. _print_style.html)."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Ширинов Отступ Отступович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)], f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    assert "text-align: justify" in body
    assert "margin-top: 6mm" in body


def test_lawsuit_print_has_letterhead_and_hides_chat_widget(db, client):
    """Исковое заявление печатается на бланке (шапка с логотипом и
    реквизитами кооператива), как остальные документы раздела, а не
    голым текстом — и без плавающего чата правления поверх превью."""
    coop = _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Бланков Бланк Бланкович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    assert "print-letterhead" in body
    assert coop.full_name in body
    assert 'id="boardChatWidget"' not in body


def test_lawsuit_print_attaches_penalty_appendix_when_accrued(db, client):
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    _board_login(db, client)
    db.add(KeyRate(rate_percent=Decimal("16.0"), effective_date=dt.date(2023, 1, 1)))
    person = make_person(db, full_name="Пенистов Пеня Пенистович")
    garage = make_garage(db, number="41")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="12000.00")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    assert "Приложение" in body
    assert "Расчёт пени" in body
    assert "Итого пени" in body


def test_lawsuit_print_no_penalty_appendix_when_not_accrued(db, client):
    """Без настроенного срока оплаты взносов (dues_due_day/month) пеня не
    считается вовсе — приложения в иске быть не должно."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Безпенистов Без Пенистович")
    garage = make_garage(db, number="42")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="12000.00")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    assert "Расчёт пени" not in body


def test_lawsuit_requires_at_least_one_person(db, client):
    _make_coop(db)
    _board_login(db, client)
    resp = client.post("/legal-docs/lawsuit/draft", data={})
    assert resp.status_code == 302


def test_lawsuit_draft_defaults_to_claim_proceeding(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Исковов Иван Петрович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/draft", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert "Исковое заявление" in body
    assert "Ответчик" in body
    assert "Взыскать с Исковова Ивана Петровича" in body
    assert "ст. 131-132" in body
    assert "судебного приказа" not in body


def test_lawsuit_draft_writ_proceeding_changes_title_party_and_wording(db, client):
    """Приказное — не просто другая цифра госпошлины: меняется заголовок,
    наименование стороны (должник, не ответчик), ссылка на ГПК РФ и
    формулировка просительной части."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Приказов Пётр Петрович")
    garage = make_garage(db, number="45")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="12000.00")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/draft", data={
        "person_id": [str(person.id)], "proceeding_type": "writ",
    })
    body = resp.get_data(as_text=True)
    assert "Заявление о вынесении судебного приказа" in body
    assert "Должник" in body
    assert "ст. 122" in body
    assert "Вынести судебный приказ о взыскании с Приказова Петра Петровича" in body


def test_lawsuit_writ_state_duty_is_half_of_claim(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Половинов Полу Полуевич")
    garage = make_garage(db, number="46")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="50000.00")
    db.commit()

    resp_claim = client.post("/legal-docs/lawsuit/draft", data={"person_id": [str(person.id)]})
    resp_writ = client.post("/legal-docs/lawsuit/draft", data={
        "person_id": [str(person.id)], "proceeding_type": "writ",
    })
    # долг 50000 <= 100000 => полная госпошлина 4000, приказная — 2000
    assert "4000,00" in resp_claim.get_data(as_text=True)
    assert "2000,00" in resp_writ.get_data(as_text=True)


def test_lawsuit_penalty_wording_cites_central_bank_methodology(db, client):
    """Пеня начисляется по методике ЦБ РФ — короткая формулировка, без
    подробного цитирования ст. 155 ЖК РФ по аналогии (упрощено по просьбе
    правления: цифры и так не совпадут с уставной формулой, объяснять это
    в тексте иска подробно незачем)."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Пенистов Пеня Петрович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/draft", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert "методике" in body and "ЦБ РФ" in body
    assert "155 Жилищного кодекса" not in body


def test_lawsuit_does_not_presuppose_defendant_is_cooperative_member(db, client):
    """Ответчик/должник может не быть членом кооператива — если его гараж
    в границах территории, где кооператив действует, взносы всё равно
    обязательны (ч. 3 ст. 27 338-ФЗ) — текст не должен безусловно
    утверждать членство ответчика."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Негагаражов Не Членович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/draft", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert "является членом кооператива и собственником" not in body
    assert "ст. 27" in body
    assert "не являющихся его членами" in body


def test_lawsuit_print_carries_proceeding_type_through_to_pdf_resubmit(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Приказнов Иван Иванович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)], "proceeding_type": "writ",
        f"text_{person.id}": "текст",
    })
    body = resp.get_data(as_text=True)
    assert "Заявление о вынесении судебного приказа" in body
    assert 'name="proceeding_type" value="writ"' in body


def test_state_duty_receipt_uses_dative_case_for_defendant_name(db, client):
    """«Государственная пошлина за подачу искового заявления к Иванову
    Ивану Ивановичу», а не буквально «к Иванов Иван Иванович» — см.
    app/name_declension.py."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Иванов Иван Иванович")
    garage = make_garage(db, number="48")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="1000.00")
    db.commit()

    resp = client.post("/legal-docs/state-duty/print", data={
        "person_id": [str(person.id)],
        f"duty_amount_{person.id}": "4000.00",
    })
    body = resp.get_data(as_text=True)
    assert "к Иванову Ивану Ивановичу" in body


def test_lawsuit_print_has_two_column_header_and_centered_title(db, client):
    """Реквизиты кооператива — слева, данные суда и ответчика — справа;
    заголовок «Исковое заявление» — отдельным центрированным жирным
    элементом, а не частью свободного текста черновика."""
    coop = _make_coop(db)
    _board_login(db, client)
    section = CourtSection(name="Судебный участок №9", court_address="г. Тестоград, ул. Судебная, 9")
    db.add(section)
    db.flush()
    person = make_person(db, full_name="Колонкин Колонка Колонкович", court_section_id=section.id)
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": "основной текст иска",
    })
    body = resp.get_data(as_text=True)
    assert "legal-header-columns" in body
    assert "legal-header-left" in body
    assert "legal-header-right" in body
    assert "Судебный участок №9" in body
    assert person.short_name in body  # в шапке — фамилия и инициалы, не полное ФИО
    assert '<p class="print-title">Исковое заявление</p>' in body
    # заголовок больше не часть редактируемого текста
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ" not in body


def test_lawsuit_print_body_paragraphs_get_indent_class(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Отступов Отступ Отступович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": "Первый абзац.\n\nВторой абзац.",
    })
    body = resp.get_data(as_text=True)
    assert '<p class="legal-p">Первый абзац.</p>' in body
    assert '<p class="legal-p">Второй абзац.</p>' in body


def test_lawsuit_print_appendix_is_signed_and_on_own_page(db, client):
    """Приложение печатается с новой страницы и заверяется той же подписью
    председателя и печатью, что и основной текст иска."""
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    board_person = make_person(db, full_name="Председателев Пред Предович")
    board_person.is_chairman = True
    make_user(db, "chair1", "pass1234", role=RoleEnum.CHAIRMAN, person=board_person)
    db.add(KeyRate(rate_percent=Decimal("16.0"), effective_date=dt.date(2023, 1, 1)))
    person = make_person(db, full_name="Заверенов Заверен Заверенович")
    garage = make_garage(db, number="43")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="12000.00")
    db.commit()
    login(client, "chair1", "pass1234")

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)],
        f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    assert body.count("stamp-place") >= 2  # печать и у основного текста, и у приложения
    assert body.count(board_person.short_name) >= 2  # подпись председателя дважды
    assert "page-break-before" in body


def test_lawsuit_signature_has_date_next_to_it(db, client):
    """Рядом с подписью председателя должна стоять дата (и у основного
    текста, и у приложения с расчётом пени, если оно печатается)."""
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    board_person = make_person(db, full_name="Председателев Пред Предович")
    board_person.is_chairman = True
    make_user(db, "chair1", "pass1234", role=RoleEnum.CHAIRMAN, person=board_person)
    db.add(KeyRate(rate_percent=Decimal("16.0"), effective_date=dt.date(2023, 1, 1)))
    person = make_person(db, full_name="Датированов Дата Датович")
    garage = make_garage(db, number="47")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="12000.00")
    db.commit()
    login(client, "chair1", "pass1234")

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)], f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    today_str = dt.date.today().strftime("%d.%m.%Y")
    assert body.count('class="sig-date"') >= 2  # у основного текста и у приложения
    assert body.count(today_str) >= 2


def test_debt_notice_signature_has_date_next_to_it(db, client):
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Датировкин Дата Датович")
    garage = make_garage(db, number="48")
    make_ownership(db, garage, person)
    _make_debt(db, person, garage, amount="1000.00")
    db.commit()

    resp = client.post("/legal-docs/debt-notice", data={"person_id": [str(person.id)]})
    body = resp.get_data(as_text=True)
    assert 'class="sig-date"' in body
    assert dt.date.today().strftime("%d.%m.%Y") in body


def test_lawsuit_header_uses_surname_and_initials_for_defendant(db, client):
    """В шапке искового заявления ответчик указан кратко — «Фамилия И.О.»,
    как и председатель в подписях — не полным ФИО."""
    _make_coop(db)
    _board_login(db, client)
    person = make_person(db, full_name="Головин Заголовок Заголовкович")
    db.commit()

    resp = client.post("/legal-docs/lawsuit/print", data={
        "person_id": [str(person.id)], f"text_{person.id}": "текст иска",
    })
    body = resp.get_data(as_text=True)
    header_idx = body.index('class="legal-header-right"')
    header_chunk = body[header_idx:header_idx + 800]
    assert person.short_name in header_chunk
    assert person.full_name not in header_chunk
