"""
app.penalty.accrue_penalties() — теперь вызывается ТОЛЬКО из
scripts/accrue_penalty.py по cron (раз в месяц), а не тихо на каждом
открытии дашборда/страницы «Пеня» — см. app/main.py, app/penalty.py.view().
Начисление происходило неочевидно для правления и раздувало историю
начислений построчно почти на каждый день при частых заходах в систему.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import RoleEnum, Cooperative, FeeType, MemberAccount, Charge, KeyRate
from app.penalty import accrue_penalties

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_overdue_charge(db):
    coop = Cooperative(full_name="ГСК Тест", inn="1", kpp="1", ogrn="1", dues_due_day=31, dues_due_month=3)
    db.add(coop)
    db.add(KeyRate(effective_date=dt.date(2025, 1, 1), rate_percent=Decimal("16.00")))
    db.flush()

    person = make_person(db, full_name="Просрочников Прос Рочкович")
    garage = make_garage(db, number="60")
    make_ownership(db, garage, person)
    regular = FeeType(code="membership_regular", name="Членский взнос", type_code="1", is_penalty=False)
    penalty = FeeType(code="membership_penalty_reg", name="Пеня по взносу", type_code="1", is_penalty=True)
    db.add_all([regular, penalty])
    db.flush()
    regular_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=regular.id, account_number="16001")
    penalty_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П16001")
    db.add_all([regular_account, penalty_account])
    db.flush()
    charge = Charge(account_id=regular_account.id, year=2025, amount=Decimal("1000.00"))
    db.add(charge)
    db.flush()
    return regular_account, penalty_account, charge


def test_accrue_penalties_charges_overdue_debt(app, db):
    regular_account, penalty_account, charge = _setup_overdue_charge(db)
    db.commit()

    result = accrue_penalties(dt.date(2025, 6, 1))

    assert "error" not in result
    assert len(result["charged_rows"]) == 1
    db.expire_all()
    penalty_charges = db.query(Charge).filter_by(account_id=penalty_account.id).all()
    assert len(penalty_charges) == 1
    assert penalty_charges[0].amount > 0
    assert penalty_charges[0].penalty_for_charge_id == charge.id


def test_accrue_penalties_is_idempotent_for_same_date(app, db):
    regular_account, penalty_account, charge = _setup_overdue_charge(db)
    db.commit()

    accrue_penalties(dt.date(2025, 6, 1))
    accrue_penalties(dt.date(2025, 6, 1))
    db.expire_all()

    penalty_charges = db.query(Charge).filter_by(account_id=penalty_account.id).all()
    assert len(penalty_charges) == 1  # повторный запуск на ту же дату не задваивает


def test_accrue_penalties_without_due_date_returns_error(app, db):
    coop = Cooperative(full_name="ГСК Тест", inn="1", kpp="1", ogrn="1")  # без dues_due_day/month
    db.add(coop)
    db.commit()

    result = accrue_penalties(dt.date(2025, 6, 1))
    assert result == {"error": "no_due_date"}


def test_accrue_penalties_without_key_rate_returns_error(app, db):
    coop = Cooperative(full_name="ГСК Тест", inn="1", kpp="1", ogrn="1", dues_due_day=31, dues_due_month=3)
    db.add(coop)
    db.commit()

    result = accrue_penalties(dt.date(2025, 6, 1))
    assert result == {"error": "no_key_rate"}


def test_dashboard_does_not_trigger_penalty_accrual(app, db, client):
    """Регрессия: раньше /dashboard тихо вызывал accrue_penalties() при
    каждом заходе правления — теперь начисление только по cron."""
    regular_account, penalty_account, charge = _setup_overdue_charge(db)
    make_user(db, "board70", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board70", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    db.expire_all()
    assert db.query(Charge).filter_by(account_id=penalty_account.id).count() == 0


def test_penalty_page_does_not_trigger_penalty_accrual(app, db, client):
    """Регрессия: раньше /finance/penalty/ тихо вызывал accrue_penalties()
    при каждом открытии — теперь начисление только по cron."""
    regular_account, penalty_account, charge = _setup_overdue_charge(db)
    make_user(db, "board71", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board71", "pass12345")

    resp = client.get("/finance/penalty/")
    assert resp.status_code == 200
    db.expire_all()
    assert db.query(Charge).filter_by(account_id=penalty_account.id).count() == 0
