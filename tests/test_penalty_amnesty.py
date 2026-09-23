"""
Амнистия пени по годам (models.PenaltyAmnesty): со срока оплаты взносов
года амнистии по дату её конца пеня не начисляется ни по каким долгам,
уже начисленная за эти дни списывается (penalty.reconcile_amnesty_write_offs),
после амнистии 1/300 первые 30 дней считается заново.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import RoleEnum, Cooperative, FeeType, MemberAccount, Charge, KeyRate, Payment, PenaltyAmnesty
from app.penalty import accrue_penalties, compute_charge_penalty_breakdown, reconcile_amnesty_write_offs

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup(db, year=2025):
    coop = Cooperative(full_name="ГСК Тест", inn="1", kpp="1", ogrn="1", dues_due_day=31, dues_due_month=3)
    db.add(coop)
    db.add(KeyRate(effective_date=dt.date(2020, 1, 1), rate_percent=Decimal("15.00")))
    db.flush()

    person = make_person(db, full_name="Амнистиев Амн Амнович")
    garage = make_garage(db, number="61")
    make_ownership(db, garage, person)
    regular = FeeType(code="membership_regular", name="Членский взнос", type_code="1", is_penalty=False)
    penalty = FeeType(code="membership_penalty_reg", name="Пеня по взносу", type_code="1", is_penalty=True)
    db.add_all([regular, penalty])
    db.flush()
    regular_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=regular.id, account_number="16101")
    penalty_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П16101")
    db.add_all([regular_account, penalty_account])
    db.flush()
    charge = Charge(account_id=regular_account.id, year=year, amount=Decimal("1000.00"))
    db.add(charge)
    db.flush()
    return coop, penalty_account, charge


def _penalty_sum(db, penalty_account):
    return sum((c.amount for c in db.query(Charge).filter_by(account_id=penalty_account.id)), Decimal("0"))


def test_no_penalty_during_amnesty_and_restart_after(app, db):
    coop, penalty_account, charge = _setup(db)
    db.add(PenaltyAmnesty(year=2025, end_date=dt.date(2025, 6, 30)))
    db.commit()

    accrue_penalties(dt.date(2025, 6, 30))
    db.expire_all()
    assert _penalty_sum(db, penalty_account) == 0

    accrue_penalties(dt.date(2025, 7, 31))
    db.expire_all()
    # 1–30 июля — 1/300 (просрочка «заново» от конца амнистии), 31 июля — уже 1/150
    expected = (Decimal("1000") * Decimal("0.15") / 300 * 30 + Decimal("1000") * Decimal("0.15") / 150).quantize(Decimal("0.01"))
    assert _penalty_sum(db, penalty_account) == expected


def test_amnesty_applies_to_older_debts_too(app, db):
    coop, penalty_account, charge = _setup(db, year=2024)
    db.add(PenaltyAmnesty(year=2025, end_date=dt.date(2025, 6, 30)))
    db.commit()

    periods = compute_charge_penalty_breakdown(charge, coop, dt.date(2025, 7, 10), [dt.date(2020, 1, 1)], [Decimal("15.00")])
    assert periods[-2]["end"] == dt.date(2025, 3, 31)
    assert periods[-1]["start"] == dt.date(2025, 7, 1)
    assert periods[-1]["divisor"] == 300  # после амнистии снова 1/300
    assert not any(p["start"] <= dt.date(2025, 5, 1) <= p["end"] for p in periods)


def test_already_accrued_penalty_is_written_off_and_restored(app, db):
    coop, penalty_account, charge = _setup(db)
    db.commit()
    accrue_penalties(dt.date(2025, 6, 30))
    db.expire_all()
    accrued = _penalty_sum(db, penalty_account)
    assert accrued > 0

    db.add(PenaltyAmnesty(year=2025, end_date=dt.date(2025, 6, 30)))
    db.flush()
    result = reconcile_amnesty_write_offs(dt.date(2025, 7, 1))
    db.commit()
    assert result["written_off"] == accrued
    write_offs = db.query(Payment).filter_by(amnesty_for_charge_id=charge.id).all()
    assert len(write_offs) == 1
    assert write_offs[0].account_id == penalty_account.id
    assert write_offs[0].amount == accrued

    # повторный прогон не задваивает
    result = reconcile_amnesty_write_offs(dt.date(2025, 7, 2))
    db.commit()
    assert result["written_off"] == 0
    assert db.query(Payment).filter_by(amnesty_for_charge_id=charge.id).count() == 1

    # амнистию убрали — списание снимается
    db.query(PenaltyAmnesty).delete()
    db.flush()
    result = reconcile_amnesty_write_offs(dt.date(2025, 7, 3))
    db.commit()
    assert result["restored"] == accrued
    assert db.query(Payment).filter_by(amnesty_for_charge_id=charge.id).count() == 0


def test_partial_amnesty_writes_off_only_amnesty_days(app, db):
    coop, penalty_account, charge = _setup(db)
    db.commit()
    accrue_penalties(dt.date(2025, 6, 30))
    db.expire_all()
    accrued = _penalty_sum(db, penalty_account)

    db.add(PenaltyAmnesty(year=2025, end_date=dt.date(2025, 4, 30)))
    db.flush()
    reconcile_amnesty_write_offs(dt.date(2025, 7, 1))
    db.commit()

    written_off = db.query(Payment).filter_by(amnesty_for_charge_id=charge.id).one().amount
    periods = compute_charge_penalty_breakdown(charge, coop, dt.date(2025, 6, 30), [dt.date(2020, 1, 1)], [Decimal("15.00")])
    corrected = sum((p["amount"] for p in periods), Decimal("0"))
    assert 0 < written_off < accrued
    assert abs(accrued - written_off - corrected) <= Decimal("0.01")


def test_amnesty_routes_privileged_only(app, db, client):
    _setup(db)
    make_user(db, "board_amn", "pass12345", role=RoleEnum.BOARD)
    make_user(db, "acc_amn", "pass12345", role=RoleEnum.ACCOUNTANT)
    db.commit()

    login(client, "board_amn", "pass12345")
    resp = client.post("/finance/penalty/amnesty", data={"year": "2025", "end_date": "2025-06-30"})
    assert resp.status_code == 403
    client.get("/auth/logout")

    login(client, "acc_amn", "pass12345")
    resp = client.post("/finance/penalty/amnesty", data={"year": "2025", "end_date": "2025-03-01"})
    assert resp.status_code == 302
    assert database.db_session.query(PenaltyAmnesty).count() == 0  # конец раньше срока оплаты

    resp = client.post("/finance/penalty/amnesty", data={"year": "2025", "end_date": "2025-06-30", "comment": "Решение ОС"})
    assert resp.status_code == 302
    amnesty = database.db_session.query(PenaltyAmnesty).one()
    assert amnesty.end_date == dt.date(2025, 6, 30)

    assert client.get("/finance/penalty/").status_code == 200

    resp = client.post(f"/finance/penalty/amnesty/{amnesty.id}/delete")
    assert resp.status_code == 302
    assert database.db_session.query(PenaltyAmnesty).count() == 0
