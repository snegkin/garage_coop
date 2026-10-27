"""
График «Собираемость взносов в течение года» на finance/member_account_stats.html
(app/finance.py: _collection_years/_collection_progress) — по выбранному
году считает % начислений (активные счета членов, без пени), собранных
НАРАСТАЮЩИМ ИТОГОМ ПО ДНЯМ, плюс дату единого по уставу срока оплаты
взносов (Cooperative.dues_due_day/dues_due_month, см.
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

    resp = client.get("/finance/member-accounts/stats")
    assert resp.status_code == 200
    assert "collectionRateChart" not in resp.get_data(as_text=True)


def test_collection_progress_cumulative_points(db):
    """Один прошлый год (2025, уже полностью в прошлом) — конечная точка
    графика зафиксирована на 31 декабря, а не «на сегодня»."""
    coop = _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Собираемость Подневная")
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
    assert progress["due_date_iso"] == "2025-06-01"
    assert progress["due_date"] == "01.06.2025"
    assert progress["total_charged"] == 1000.0
    assert progress["points"] == [
        {"date": "2025-01-01", "rate": 0.0},
        {"date": "2025-03-01", "rate": 30.0},
        {"date": "2025-08-01", "rate": 50.0},
        {"date": "2025-12-31", "rate": 50.0},
    ]


def test_collection_progress_multiple_payments_same_day_summed(db):
    coop = _make_coop(db)
    person = make_person(db, full_name="Два Платежа В День")
    garage = make_garage(db, number="96")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col6")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 3, 1), amount=Decimal("100.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 3, 1), amount=Decimal("150.00")))
    db.flush()
    reallocate_member_charges(account)
    db.commit()

    progress = _collection_progress(coop, 2025)
    day_points = [p for p in progress["points"] if p["date"] == "2025-03-01"]
    assert len(day_points) == 1
    assert day_points[0]["rate"] == 25.0


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
    assert all(p["rate"] == 0.0 for p in progress["points"])


def test_collection_progress_no_due_date_when_not_configured(db):
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
    assert progress["due_date_iso"] is None
    assert progress["due_date"] is None
    assert progress["points"][-1]["rate"] == 100.0


def test_collection_progress_returns_none_for_year_without_charges(db):
    coop = _make_coop(db)
    assert _collection_progress(coop, 2025) is None


def test_collection_progress_flat_line_when_no_payments_at_all(db):
    coop = _make_coop(db)
    person = make_person(db, full_name="Без Единого Платежа")
    garage = make_garage(db, number="97")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col7")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))
    db.flush()
    reallocate_member_charges(account)
    db.commit()

    progress = _collection_progress(coop, 2025)
    assert progress["points"] == [
        {"date": "2025-01-01", "rate": 0.0},
        {"date": "2025-12-31", "rate": 0.0},
    ]


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

    resp = client.get("/finance/member-accounts/stats")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "collectionRateChart" in body
    assert 'id="collectionYearSelect"' in body
    assert '<option value="2024"' in body
    # По умолчанию выбран последний год с начислениями
    assert '<option value="2025" selected' in body

    resp2 = client.get("/finance/member-accounts/stats?collection_year=2024")
    body2 = resp2.get_data(as_text=True)
    assert '<option value="2024" selected' in body2


# ---------------------------------------------------------------------------
# Переключение года без перезагрузки страницы — JSON-эндпоинт
# ---------------------------------------------------------------------------

def test_collection_progress_data_endpoint_returns_json(db, client):
    _make_coop(db, dues_due_day=1, dues_due_month=6)
    person = make_person(db, full_name="Аякс Годовой Аяксович")
    garage = make_garage(db, number="98")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col8")
    db.add(Charge(account_id=account.id, year=2024, amount=Decimal("100.00")))
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("200.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2024, 5, 1), amount=Decimal("100.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts/collection-progress?year=2024")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["year"] == 2024
    assert data["points"][-1]["rate"] == 100.0


def test_collection_progress_data_endpoint_rejects_unknown_year(db, client):
    _make_coop(db)
    person = make_person(db, full_name="Год Без Начислений")
    garage = make_garage(db, number="99")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "col9")
    db.add(Charge(account_id=account.id, year=2024, amount=Decimal("100.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    resp = client.get("/finance/member-accounts/collection-progress?year=1999")
    assert resp.status_code == 404


def test_collection_progress_data_endpoint_requires_board(db, client):
    _make_coop(db)
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get("/finance/member-accounts/collection-progress?year=2024")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Разбивка по видам взноса под графиком и вынос на отдельную страницу
# ---------------------------------------------------------------------------

def test_collection_progress_by_type_breakdown(db):
    """Итог разбивки совпадает с последней точкой графика: платежи
    следующего года в «собрано» этого года не входят, остаются долгом."""
    coop = _make_coop(db)
    person = make_person(db, full_name="Разбивка По Видам")
    garage = make_garage(db, number="88")
    make_ownership(db, garage, person)
    membership = _make_account(db, person, garage, "bt1")
    land_tax_type = FeeType(code="bt2", name="Земельный налог")
    db.add(land_tax_type)
    db.flush()
    land_tax = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=land_tax_type.id, account_number="Cbt2",
    )
    db.add(land_tax)
    db.flush()
    db.add(Charge(account_id=membership.id, year=2025, amount=Decimal("1000.00")))
    db.add(Payment(account_id=membership.id, date=dt.date(2025, 3, 1), amount=Decimal("600.00")))
    db.add(Charge(account_id=land_tax.id, year=2025, amount=Decimal("200.00")))
    db.add(Payment(account_id=land_tax.id, date=dt.date(2026, 1, 10), amount=Decimal("200.00")))
    db.flush()
    reallocate_member_charges(membership)
    reallocate_member_charges(land_tax)
    db.commit()

    progress = _collection_progress(coop, 2025)
    assert progress["by_type"] == [
        {"name": "Земельный налог", "charged": 200.0, "collected": 0.0, "debt": 200.0, "rate": 0.0},
        {"name": "Членский взнос", "charged": 1000.0, "collected": 600.0, "debt": 400.0, "rate": 60.0},
    ]
    assert progress["total"] == {"name": "", "charged": 1200.0, "collected": 600.0, "debt": 600.0, "rate": 50.0}
    assert progress["points"][-1]["rate"] == progress["total"]["rate"]


def test_stats_moved_off_member_accounts_list(db, client):
    _make_coop(db)
    person = make_person(db, full_name="Перенос Статистики")
    garage = make_garage(db, number="87")
    make_ownership(db, garage, person)
    account = _make_account(db, person, garage, "mv1")
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("100.00")))
    db.flush()
    reallocate_member_charges(account)
    _board_login(db, client)

    body = client.get("/finance/member-accounts").get_data(as_text=True)
    assert "collectionRateChart" not in body
    assert "Статистика по видам счетов" not in body

    stats = client.get("/finance/member-accounts/stats").get_data(as_text=True)
    assert 'id="collectionBreakdown"' in stats
    assert "100,00" in stats.split('id="collectionBreakdown"')[1]


def test_stats_page_requires_board(db, client):
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member2", "pass12345")
    assert client.get("/finance/member-accounts/stats").status_code == 302
