"""
Гаражи за территорией кооператива (Garage.outside_territory): отдельный
договор, электроэнергия по тарифу для членов плюс потери в линии
(ElectricitySettings.outside_line_loss_percent), а взносы — только те
виды, что отмечены в форме гаража (остальные — Garage.excluded_fee_types,
не начисляются массовым начислением и не участвуют в земельном налоге).
"""
import datetime as dt
from decimal import Decimal

from app.accounting import compute_land_tax, get_electricity_settings
from app.models import (
    RoleEnum, Charge, Cooperative, ElectricityMeter, ElectricityReading, ElectricityTariff, ElectricityTariffKind,
    FeeType, Garage, MemberAccount,
)

from tests.conftest import make_garage, make_person, make_ownership, make_user, login


def _fee_types(db):
    membership = FeeType(code="membership", name="Членский взнос", type_code="10")
    target = FeeType(code="target", name="Целевой взнос", type_code="20")
    land_tax = FeeType(code="land_tax", name="Земельный налог", type_code="30")
    db.add_all([membership, target, land_tax])
    db.flush()
    return membership, target, land_tax


def test_create_outside_garage_with_only_target_fee(db, client):
    membership, target, land_tax = _fee_types(db)
    make_user(db, "board_out1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_out1", "pass12345")

    resp = client.post("/garages/new", data={
        "number": "В-1", "area_sqm": "20", "coefficient": "1",
        "outside_territory": "on", "charged_fee_type_id": [str(target.id)],
    })
    assert resp.status_code == 302
    garage = db.query(Garage).filter_by(number="В-1").one()
    assert garage.outside_territory is True
    assert {ft.id for ft in garage.excluded_fee_types} == {membership.id, land_tax.id}

    # снятие флажка «за территорией» убирает все исключения
    resp = client.post(f"/garages/{garage.id}/edit", data={"number": "В-1", "area_sqm": "20", "coefficient": "1"})
    assert resp.status_code == 302
    db.expire_all()
    garage = db.get(Garage, garage.id)
    assert garage.outside_territory is False
    assert garage.excluded_fee_types == []


def test_mass_charge_skips_excluded_and_charges_included_with_coefficient(db, client):
    membership, target, _land_tax = _fee_types(db)
    person = make_person(db, full_name="Загородный Гараж Гаражевич")
    inside = make_garage(db, number="50")
    outside = make_garage(db, number="51", coefficient=Decimal("0.5"), outside_territory=True)
    outside.excluded_fee_types = [membership]
    accounts = {}
    for garage in (inside, outside):
        make_ownership(db, garage, person)
        for ft in (membership, target):
            acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=ft.id,
                                account_number=f"{ft.type_code}{garage.number}")
            db.add(acc)
            accounts[(garage.number, ft.code)] = acc
    make_user(db, "chair_out", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_out", "pass12345")

    for ft in (membership, target):
        resp = client.post("/finance/mass-charge", data={
            "year": "2026", "strategy": "coefficient", "fee_type_id": str(ft.id),
            "base_amount": "1000", "round_up": "0",
        })
        assert resp.status_code == 200
    db.expire_all()

    def charged(number, code):
        return [c.amount for c in db.query(Charge).filter_by(account_id=accounts[(number, code)].id)]

    assert charged("50", "membership") == [Decimal("1000.00")]
    assert charged("51", "membership") == []
    assert charged("50", "target") == [Decimal("1000.00")]
    assert charged("51", "target") == [Decimal("500.00")]  # целевой — с коэффициентом гаража


def test_land_tax_excluded_garage_not_in_common_area_split(db):
    _membership, _target, land_tax = _fee_types(db)
    db.add(Cooperative(
        full_name="ГК", inn="1234567890", kpp="123401001", ogrn="1023400000000", cadastral_area=Decimal("1000"), cadastral_value=Decimal("100000"),
        common_area=Decimal("100"), land_tax_rate_percent=Decimal("1"), standard_garage_land_area=Decimal("30"),
    ))
    a = make_garage(db, number="60")
    b = make_garage(db, number="61")
    outside = make_garage(db, number="62", outside_territory=True)
    outside.excluded_fee_types = [land_tax]
    db.commit()

    result = compute_land_tax(2026)
    assert set(result) == {a.id, b.id}
    # налог 1000 ₽ / 1000 м² = 1 ₽/м²: 30 м² под гаражом + 100 м² общей / 2 гаража
    assert result[a.id] == Decimal("80.00")


def test_outside_garage_electricity_with_line_loss(db, client):
    garage = make_garage(db, number="70", outside_territory=True)
    meter = ElectricityMeter(garage_id=garage.id, meter_number="m-70", initial_reading=Decimal("0"),
                             installed_date=dt.date(2026, 1, 1))
    db.add(meter)
    db.add(FeeType(code="electricity", name="Электричество"))
    db.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("6"), effective_date=dt.date(2020, 1, 1)))
    get_electricity_settings().outside_line_loss_percent = Decimal("2.33")
    make_user(db, "board_out2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_out2", "pass12345")

    resp = client.post(f"/garages/{garage.id}/electricity/reading/add", data={"reading": "100"})
    assert resp.status_code == 302
    reading = db.query(ElectricityReading).filter_by(meter_id=meter.id).one()
    assert reading.tariff == Decimal("6.1398")  # 6 × 1,0233
    assert db.query(Charge).filter_by(reading_id=reading.id).one().amount == Decimal("613.98")


def test_board_sets_line_loss_percent(db, client):
    make_user(db, "board_out3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_out3", "pass12345")
    resp = client.post("/power/outside-line-loss", data={"outside_line_loss_percent": "3,1"})
    assert resp.status_code == 302
    db.expire_all()
    assert get_electricity_settings().outside_line_loss_percent == Decimal("3.10")


def test_pages_render_with_outside_garage(db, client):
    membership, target, _land_tax = _fee_types(db)
    garage = make_garage(db, number="80", outside_territory=True)
    garage.excluded_fee_types = [membership]
    db.add(ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("6"), effective_date=dt.date(2020, 1, 1)))
    make_user(db, "chair_out2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_out2", "pass12345")

    detail = client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert "За территорией" in detail and "Целевой взнос" in detail
    edit = client.get(f"/garages/{garage.id}/edit").get_data(as_text=True)
    assert f'value="{target.id}" id="chargedFee{target.id}"\n           checked' in edit
    for url in ("/garages/", "/garages/new", "/finance/mass-charge"):
        assert client.get(url).status_code == 200, url
    power = client.get("/power/").get_data(as_text=True)
    assert "6.1398" in power
