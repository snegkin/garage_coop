"""
График «Собираемость взносов по годам» на finance/member_accounts.html
(app/finance.py: _collection_timeline) — по годам считает % начислений
по активным счетам членов (без пени), которые уже оплачены на сегодня,
и отдельно — какая доля оплачена платежами не позже единого по уставу
срока оплаты взносов (Cooperative.dues_due_day/dues_due_month, см.
accounting.dues_due_date).
"""
import datetime as dt
from decimal import Decimal

from app.accounting import reallocate_member_charges
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, Cooperative

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_coop(db, **kwargs):
    coop = Cooperative(
        full_name="Тестовый гаражный кооператив", inn="1234567890", kpp="123456789", ogrn="1234567890123",
        **kwargs,
    )
    db.add(coop)
    db.flush()
    return coop


def _make_account(db, person, garage, code):
    fee_type = FeeType(code=code, name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=f"C{code}",
    )
    db.add(account)
    db.flush()
    return account


def _board_login(db, client, username="board1"):
    make_user(db, username, "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, username, "pass12345")


def test_chart_absent_when_no_charges(db, client):
    _make_coop(db, dues_due_day=1, dues_due_month=6)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    assert "collectionRateChart" not in resp.get_data(as_text=True)


def test_chart_shows_on_time_and_total_rates(db, client):
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Собираемость Годовая")
    garage = make_garage(db, number="90")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col1")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))
    # 300 оплачено ДО срока (1 июня), ещё 200 — уже ПОСЛЕ срока
    db.add(Payment(account_id=account.id, date=dt.date(2025, 3, 1), amount=Decimal("300.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 8, 1), amount=Decimal("200.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "collectionRateChart" in body
    assert '"year": 2025' in body
    assert '"rate_by_due_date": 30.0' in body
    assert '"rate_total": 50.0' in body
    assert '"due_date": "01.06.2025"' in body


def test_chart_shows_note_when_due_date_not_configured(db, client):
    _make_coop(db)  # dues_due_day/month не заданы
    person = make_person(db, full_name="Без Срока Оплаты")
    garage = make_garage(db, number="91")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col2")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("500.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 3, 1), amount=Decimal("500.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts")
    body = resp.get_data(as_text=True)
    assert "не задан в реквизитах кооператива" in body
    assert '"due_date": null' in body
    assert '"rate_by_due_date": null' in body
    assert '"rate_total": 100.0' in body


def test_archived_accounts_excluded_from_timeline(db, client):
    _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Архивный Счёт Годовой")
    garage = make_garage(db, number="92")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="col3", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id,
        account_number="C-col3", is_archived=True,
    )
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2024, amount=Decimal("1000.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts")
    body = resp.get_data(as_text=True)
    assert "collectionRateChart" not in body


def test_penalty_accounts_excluded_from_timeline(db, client):
    _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Пеня Годовая")
    garage = make_garage(db, number="93")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="pen1", name="Пеня", is_penalty=True)
    db.add(fee_type)
    db.flush()
    account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="C-pen1",
    )
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2024, amount=Decimal("100.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts")
    body = resp.get_data(as_text=True)
    assert "collectionRateChart" not in body
