"""
Показания счётчика гаража задним числом (app/garages.py:
add_electricity_reading, поле reading_date) — только для правления и не
раньше последнего показания/установки счётчика.
"""
import datetime as dt
from decimal import Decimal

from app.models import (
    RoleEnum, ElectricityMeter, ElectricityReading, ElectricityTariff, ElectricityTariffKind, FeeType, Charge,
)

from tests.conftest import make_garage, make_person, make_ownership, make_user, login


TODAY = dt.date.today()


def _setup(db):
    garage = make_garage(db, number="7")
    meter = ElectricityMeter(
        garage_id=garage.id, meter_number="m-7",
        initial_reading=Decimal("1000"), installed_date=TODAY - dt.timedelta(days=60),
    )
    db.add(meter)
    db.add(FeeType(code="electricity", name="Электричество"))
    # тариф сменился 5 дней назад — показание задним числом должно
    # посчитаться по тарифу, действовавшему на дату снятия
    db.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("10"), effective_date=dt.date(2020, 1, 1)))
    db.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("20"), effective_date=TODAY - dt.timedelta(days=5)))
    db.flush()
    db.add(ElectricityReading(meter_id=meter.id, reading=Decimal("1050"), reading_date=TODAY - dt.timedelta(days=30)))
    make_user(db, "board_bd", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    return garage, meter


def _new_readings(db, meter):
    return db.query(ElectricityReading).filter_by(meter_id=meter.id).filter(ElectricityReading.reading != Decimal("1050")).all()


def test_board_adds_backdated_reading_with_tariff_on_that_date(db, client):
    garage, meter = _setup(db)
    login(client, "board_bd", "pass12345")
    reading_date = TODAY - dt.timedelta(days=10)

    resp = client.post(
        f"/garages/{garage.id}/electricity/reading/add",
        data={"reading": "1100", "reading_date": reading_date.isoformat()},
    )
    assert resp.status_code == 302

    reading = _new_readings(db, meter)[0]
    assert reading.reading_date == reading_date
    assert reading.tariff == Decimal("10")
    charge = db.query(Charge).filter_by(reading_id=reading.id).one()
    assert charge.amount == Decimal("500.00")  # (1100-1050) × 10
    assert charge.year == reading_date.year


def test_backdated_reading_before_previous_is_rejected(db, client):
    garage, meter = _setup(db)
    login(client, "board_bd", "pass12345")

    client.post(
        f"/garages/{garage.id}/electricity/reading/add",
        data={"reading": "1100", "reading_date": (TODAY - dt.timedelta(days=31)).isoformat()},
    )
    assert _new_readings(db, meter) == []


def test_future_reading_date_is_rejected(db, client):
    garage, meter = _setup(db)
    login(client, "board_bd", "pass12345")

    client.post(
        f"/garages/{garage.id}/electricity/reading/add",
        data={"reading": "1100", "reading_date": (TODAY + dt.timedelta(days=1)).isoformat()},
    )
    assert _new_readings(db, meter) == []


def test_owner_cannot_backdate_reading(db, client):
    """Собственнику поле даты не показывается, а присланное вручную
    игнорируется — показания ложатся на сегодня."""
    garage, meter = _setup(db)
    owner = make_person(db, full_name="Собственников Собственник")
    make_ownership(db, garage, owner)
    make_user(db, "owner_bd", "pass12345", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    login(client, "owner_bd", "pass12345")

    html = client.get(f"/garages/{garage.id}?tab=account").get_data(as_text=True)
    assert 'name="reading_date"' not in html

    client.post(
        f"/garages/{garage.id}/electricity/reading/add",
        data={"reading": "1100", "reading_date": (TODAY - dt.timedelta(days=10)).isoformat()},
    )
    assert _new_readings(db, meter)[0].reading_date == TODAY


def test_board_sees_reading_date_field(db, client):
    garage, _meter = _setup(db)
    login(client, "board_bd", "pass12345")

    html = client.get(f"/garages/{garage.id}?tab=account").get_data(as_text=True)
    assert 'name="reading_date"' in html
    assert f'min="{(TODAY - dt.timedelta(days=30)).isoformat()}"' in html
