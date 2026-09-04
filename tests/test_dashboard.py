"""
Панель кооператива (/dashboard) для правления/председателя — сводка долгов
(app.main._debt_summary), блок «Требует внимания» (использует уже
посчитанные в контекст-процессоре pending_pd_count/pending_votes_count/
pending_proposals_count, см. app/__init__.py) и недавняя активность
(последние записи журнала аудита).
"""
import datetime as dt
from decimal import Decimal

from app import audit
from app.accounting import reallocate_member_charges
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment

from tests.conftest import make_person, make_garage, make_ownership, make_user, login
from tests.test_mailbox import _mock_imap, _make_settings, _test_email


def _make_account(db, person, garage, code, is_archived=False):
    fee_type = FeeType(code=code, name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id,
        account_number=f"D{code}", is_archived=is_archived,
    )
    db.add(account)
    db.flush()
    return account


def test_dashboard_shows_total_debt_and_account_count(app, db, client):
    person = make_person(db, full_name="Должников Должник Должникович")
    garage = make_garage(db, number="70")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "dash1")
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("1000.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("400.00")))
    make_user(db, "board100", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board100", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "600,00" in body  # долг 1000 - 400 = 600
    assert "1 счетов с долгом" in body or "1 счет" in body or ">1<" in body


def test_dashboard_ignores_archived_accounts_in_debt(app, db, client):
    person = make_person(db, full_name="Архивников Ар Хивович")
    garage = make_garage(db, number="71")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "dash2", is_archived=True)
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("5000.00")))
    make_user(db, "board101", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board101", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "нет должников" in resp.get_data(as_text=True)


def test_dashboard_shows_no_debtors_when_none(app, db, client):
    make_user(db, "board102", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board102", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "нет должников" in resp.get_data(as_text=True)


def test_dashboard_shows_recent_activity(app, db, client):
    make_user(db, "chair100", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    audit.record("payment.create", entity_type="member_account", entity_id=1, summary="Тестовая запись журнала аудита №1")
    db.commit()
    login(client, "chair100", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Тестовая запись журнала аудита №1" in body
    assert "Недавняя активность" in body


def test_dashboard_hides_pending_section_when_nothing_pending(app, db, client):
    make_user(db, "board103", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board103", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Требует внимания" not in resp.get_data(as_text=True)


def test_dashboard_debt_card_links_to_member_accounts(app, db, client):
    make_user(db, "board104", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board104", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'href="/finance/member-accounts"' in resp.get_data(as_text=True)


def test_dashboard_shows_collection_rate_for_current_and_previous_year(app, db, client):
    current_year = dt.date.today().year
    person = make_person(db, full_name="Собираемость Проверяемая")
    garage = make_garage(db, number="72")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "dash3")
    db.add(Charge(account_id=account.id, year=current_year, amount=Decimal("1000.00")))
    db.add(Payment(account_id=account.id, date=dt.date(current_year, 1, 1), amount=Decimal("250.00")))
    db.flush()
    reallocate_member_charges(account)

    account2 = _make_account(db, person, garage, "dash4")
    db.add(Charge(account_id=account2.id, year=current_year - 1, amount=Decimal("800.00")))
    db.add(Payment(account_id=account2.id, date=dt.date(current_year - 1, 6, 1), amount=Decimal("800.00")))
    db.flush()
    reallocate_member_charges(account2)

    make_user(db, "board105", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board105", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Собираемость" in body
    assert f">{current_year}</span>" in body
    assert "25,00%</span>" in body
    assert f">{current_year - 1}</span>" in body
    assert "100,00%</span>" in body


def test_dashboard_shows_collection_rate_for_three_years_but_not_older(app, db, client):
    current_year = dt.date.today().year
    person = make_person(db, full_name="Три Года Проверяемая")
    garage = make_garage(db, number="73")
    make_ownership(db, garage, person)

    for offset, amount, paid in ((0, "1000.00", "1000.00"), (1, "1000.00", "500.00"), (2, "1000.00", "250.00"), (3, "1000.00", "1000.00")):
        account = _make_account(db, person, garage, f"dash-y{offset}")
        year = current_year - offset
        db.add(Charge(account_id=account.id, year=year, amount=Decimal(amount)))
        db.add(Payment(account_id=account.id, date=dt.date(year, 6, 1), amount=Decimal(paid)))
        db.flush()
        reallocate_member_charges(account)

    make_user(db, "board107", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board107", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f">{current_year}</span>" in body
    assert "100,00%</span>" in body
    assert f">{current_year - 1}</span>" in body
    assert "50,00%</span>" in body
    assert f">{current_year - 2}</span>" in body
    assert "25,00%</span>" in body
    assert f">{current_year - 3}</span>" not in body  # только 3 года — за 4-й собираемость не выводится


def test_dashboard_hides_collection_rate_when_nothing_charged(app, db, client):
    make_user(db, "board106", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board106", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Собираемость" not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Виджет «Письма» (превью входящих на панели, см. app/main.py: _mail_preview)
# ---------------------------------------------------------------------------

def test_dashboard_mail_widget_shows_not_configured_message(app, db, client):
    make_user(db, "board108", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board108", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Почта ещё не настроена." in resp.get_data(as_text=True)


def test_dashboard_mail_widget_lists_recent_messages(app, db, client, monkeypatch):
    _make_settings(db)
    _mock_imap(monkeypatch, {1: _test_email(subject="Уникальная тема панели").as_bytes()})
    make_user(db, "board109", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board109", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Уникальная тема панели" in body
    assert "Отправитель" in body


def test_dashboard_mail_widget_connection_error_does_not_break_page(app, db, client, monkeypatch):
    from app import mail_client

    _make_settings(db)

    def boom(settings):
        raise mail_client.MailError("connection refused")
    monkeypatch.setattr(mail_client, "_connect_imap", boom)
    make_user(db, "board110", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board110", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Не удалось подключиться к почте." in resp.get_data(as_text=True)
