"""
График «Собираемость взносов в течение года» на finance/member_accounts.html
(app/finance.py: _collection_years/_collection_progress) — по выбранному
году считает % начислений (активные счета членов, без пени), собранных
НАРАСТАЮЩИМ ИТОГОМ к концу каждого месяца, плюс месяц единого по уставу
срока оплаты взносов (Cooperative.dues_due_day/dues_due_month, см.
accounting.dues_due_date) для пунктирной отметки на графике.
"""
import datetime as dt
from decimal import Decimal

from app.accounting import reallocate_member_charges
from app.finance import _collection_years, _collection_progress
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


def test_collection_progress_cumulative_by_month(db):
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Собираемость Помесячная")
    garage = make_garage(db, number="90")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col1")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 3, 1), amount=Decimal("300.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 8, 1), amount=Decimal("200.00")))
    db.flush()
    reallocate_member_charges(account)
    db.commit()

    progress = _collection_progress(coop, 2025)
    assert progress["year"] == 2025
    assert progress["due_month"] == 6
    assert progress["due_date"] == "01.06.2025"
    assert progress["total_charged"] == 1000.0
    # Нарастающим итогом: 300 с марта (30%) держится по июль, с августа — 500 (50%)
    assert progress["cumulative_rate"] == [
        0.0, 0.0, 30.0, 30.0, 30.0, 30.0, 30.0, 50.0, 50.0, 50.0, 50.0, 50.0,
    ]


def test_collection_progress_ignores_payments_from_other_years(db):
    """Платёж, сделанный уже в СЛЕДУЮЩЕМ году (просрочка через границу
    года), не попадает в кривую ЭТОГО года — она про то, что произошло
    внутри него."""
    coop = _make_coop(db)
    person = make_person(db, full_name="Просрочка Через Год")
    garage = make_garage(db, number="94")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col4")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2026, 1, 15), amount=Decimal("1000.00")))
    db.flush()
    reallocate_member_charges(account)
    db.commit()

    progress = _collection_progress(coop, 2025)
    assert progress["cumulative_rate"] == [0.0] * 12


def test_collection_progress_none_when_due_date_not_configured(db):
    coop = _make_coop(db)  # dues_due_day/month не заданы
    person = make_person(db, full_name="Без Срока Оплаты")
    garage = make_garage(db, number="91")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col2")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("500.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 3, 1), amount=Decimal("500.00")))
    db.flush()
    reallocate_member_charges(account)
    db.commit()

    progress = _collection_progress(coop, 2025)
    assert progress["due_month"] is None
    assert progress["due_date"] is None
    assert progress["cumulative_rate"][-1] == 100.0


def test_collection_progress_returns_none_for_year_without_charges(db):
    coop = _make_coop(db)
    assert _collection_progress(coop, 2025) is None


def test_archived_accounts_excluded_from_years(db):
    _make_coop(db)
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
    db.commit()

    assert _collection_years() == []


def test_penalty_accounts_excluded_from_years(db):
    _make_coop(db)
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
    db.commit()

    assert _collection_years() == []


def test_page_shows_chart_and_year_selector(db, client):
    _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Селектор Года Годович")
    garage = make_garage(db, number="95")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col5")
    db.add(Charge(account_id=account.id, year=2024, amount=Decimal("100.00")))
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("200.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "collectionRateChart" in body
    assert 'name="collection_year"' in body
    assert '<option value="2024"' in body
    # По умолчанию выбран последний год с начислениями
    assert '<option value="2025" selected' in body

    resp2 = client.get("/finance/member-accounts?collection_year=2024")
    body2 = resp2.get_data(as_text=True)
    assert '<option value="2024" selected' in body2
