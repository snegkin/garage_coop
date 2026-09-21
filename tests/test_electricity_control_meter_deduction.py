"""
Вычитание контрольного узла из начисления за электричество абоненту
(app/garages.py: add_electricity_reading/edit_last_reading), когда узел
физически запитан через щиток этого гаража (ControlMeter.parent_garage_id,
см. app/control_meters.py: garage_supplied_nodes_delta). Дерево контрольных
счётчиков само по себе покрыто tests/test_control_meters.py — здесь только
интеграция с начислением.
"""
import datetime as dt
from decimal import Decimal

from app.models import (
    RoleEnum, ControlMeter, ControlMeterReading, ElectricityMeter, ElectricityReading,
    ElectricityTariff, ElectricityTariffKind, FeeType, Charge,
)

from tests.conftest import make_garage, make_user, login


def _make_board(db, username="board1"):
    make_user(db, username, "pass1234", role=RoleEnum.BOARD)
    db.commit()


def _setup_garage_with_meter(db, number, initial_reading, installed_date):
    garage = make_garage(db, number=number)
    meter = ElectricityMeter(
        garage_id=garage.id, meter_number=f"m-{garage.id}",
        initial_reading=Decimal(initial_reading), installed_date=installed_date,
    )
    db.add(meter)
    db.flush()
    return garage, meter


def _make_tariff(db, rate="10"):
    tariff = ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal(rate), effective_date=dt.date(2020, 1, 1))
    db.add(tariff)
    db.flush()
    return tariff


def _make_fee_type(db):
    ft = FeeType(code="electricity", name="Электричество")
    db.add(ft)
    db.flush()
    return ft


def test_charge_nets_out_garage_supplied_node_delta(db, client):
    _make_board(db)
    installed = dt.date.today() - dt.timedelta(days=30)
    garage, meter = _setup_garage_with_meter(db, "1", "1000", installed)
    _make_tariff(db, rate="10")
    _make_fee_type(db)

    node = ControlMeter(name="Освещение", parent_garage_id=garage.id)
    db.add(node)
    db.flush()
    db.add(ControlMeterReading(control_meter_id=node.id, reading=Decimal("0"), reading_date=installed))
    db.add(ControlMeterReading(control_meter_id=node.id, reading=Decimal("30"), reading_date=dt.date.today()))
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/garages/{garage.id}/electricity/reading/add", data={"reading": "1100", "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200

    reading = db.query(ElectricityReading).filter_by(meter_id=meter.id).one()
    assert reading.control_meter_deduction == Decimal("30")
    charge = db.query(Charge).filter_by(garage_id=garage.id).one()
    # (1100-1000) - 30 = 70, при тарифе 10 -> 700
    assert charge.amount == Decimal("700.00")
    assert "30" in reading.comment


def test_charge_uses_full_delta_when_supplied_node_has_no_fresh_reading(db, client):
    """Узел без свежих показаний на нужную дату не блокирует начисление —
    вычитается 0, но абонент/председатель должны увидеть предупреждение в
    комментарии (is_partial), иначе начисление молча занижает вычет."""
    _make_board(db)
    installed = dt.date.today() - dt.timedelta(days=30)
    garage, meter = _setup_garage_with_meter(db, "2", "1000", installed)
    _make_tariff(db, rate="10")
    _make_fee_type(db)

    # у узла вовсе нет показаний — _node_reading_as_of всегда None
    node = ControlMeter(name="Освещение", parent_garage_id=garage.id)
    db.add(node)
    db.flush()
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/garages/{garage.id}/electricity/reading/add", data={"reading": "1100", "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200

    reading = db.query(ElectricityReading).filter_by(meter_id=meter.id).one()
    assert reading.control_meter_deduction is None
    charge = db.query(Charge).filter_by(garage_id=garage.id).one()
    assert charge.amount == Decimal("1000.00")  # полная дельта 100 * 10, без вычета
    assert "не вычет" in reading.comment.lower() or "занижен" in reading.comment.lower()


def test_charge_unaffected_when_no_supplied_nodes(db, client):
    """Обычный гараж без запитанных узлов — поведение не меняется."""
    _make_board(db)
    installed = dt.date.today() - dt.timedelta(days=30)
    garage, meter = _setup_garage_with_meter(db, "3", "1000", installed)
    _make_tariff(db, rate="10")
    _make_fee_type(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/garages/{garage.id}/electricity/reading/add", data={"reading": "1100", "comment": ""}, follow_redirects=True)
    assert resp.status_code == 200

    reading = db.query(ElectricityReading).filter_by(meter_id=meter.id).one()
    assert reading.control_meter_deduction is None
    charge = db.query(Charge).filter_by(garage_id=garage.id).one()
    assert charge.amount == Decimal("1000.00")
