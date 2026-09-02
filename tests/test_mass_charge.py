"""
Массовое начисление по стратегии «По коэффициенту гаража»
(finance.mass_charge, strategy == "coefficient") — для вида взноса
«Земельный налог» форма принимает ДВЕ суммы (см. mass_charge.html):
base_amount — для гаражей с неприватизированной землёй, base_amount_privatized
— для гаражей с приватизированной (Garage.land_privatized). Для остальных
видов взноса поведение не меняется — одна сумма на все гаражи.
"""
from decimal import Decimal

from app import database
from app.models import RoleEnum, FeeType, Garage, GarageOwnership, MemberAccount, Charge

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_mass_charge_coefficient_land_tax_uses_privatized_amount(app, db, client):
    person = make_person(db, full_name="Землевладелец Гараж Гаражевич")
    garage_privatized = make_garage(db, number="70", coefficient=Decimal("1.5"), land_privatized=True)
    garage_regular = make_garage(db, number="71", coefficient=Decimal("2"), land_privatized=False)
    make_ownership(db, garage_privatized, person)
    make_ownership(db, garage_regular, person)

    fee_type = FeeType(code="land_tax", name="Земельный налог")
    db.add(fee_type)
    db.flush()
    account_privatized = MemberAccount(
        person_id=person.id, garage_id=garage_privatized.id, fee_type_id=fee_type.id, account_number="70001",
    )
    account_regular = MemberAccount(
        person_id=person.id, garage_id=garage_regular.id, fee_type_id=fee_type.id, account_number="71001",
    )
    db.add_all([account_privatized, account_regular])
    make_user(db, "chair30", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair30", "pass12345")

    resp = client.post("/finance/mass-charge", data={
        "year": "2026", "strategy": "coefficient", "fee_type_id": str(fee_type.id),
        "base_amount": "1000", "base_amount_privatized": "300", "round_up": "0",
    })
    assert resp.status_code == 200
    db.expire_all()

    charge_privatized = db.query(Charge).filter_by(account_id=account_privatized.id).one()
    charge_regular = db.query(Charge).filter_by(account_id=account_regular.id).one()
    # приватизированный: 300 (base_amount_privatized) × 1.5 (коэфф.) = 450
    assert charge_privatized.amount == Decimal("450.00")
    # обычный: 1000 (base_amount) × 2 (коэфф.) = 2000
    assert charge_regular.amount == Decimal("2000.00")


def test_mass_charge_coefficient_non_land_tax_uses_single_amount(app, db, client):
    """Регрессия: для видов взноса, отличных от земельного налога, поле
    base_amount_privatized не участвует в расчёте вовсе — сумма едина для
    всех гаражей, приватизирован участок или нет."""
    person = make_person(db, full_name="Взносов Иван Иванович")
    garage_privatized = make_garage(db, number="72", coefficient=Decimal("1"), land_privatized=True)
    garage_regular = make_garage(db, number="73", coefficient=Decimal("1"), land_privatized=False)
    make_ownership(db, garage_privatized, person)
    make_ownership(db, garage_regular, person)

    fee_type = FeeType(code="membership", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account_privatized = MemberAccount(
        person_id=person.id, garage_id=garage_privatized.id, fee_type_id=fee_type.id, account_number="72001",
    )
    account_regular = MemberAccount(
        person_id=person.id, garage_id=garage_regular.id, fee_type_id=fee_type.id, account_number="73001",
    )
    db.add_all([account_privatized, account_regular])
    make_user(db, "chair31", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair31", "pass12345")

    resp = client.post("/finance/mass-charge", data={
        "year": "2026", "strategy": "coefficient", "fee_type_id": str(fee_type.id),
        "base_amount": "500", "round_up": "0",
    })
    assert resp.status_code == 200
    db.expire_all()

    charge_privatized = db.query(Charge).filter_by(account_id=account_privatized.id).one()
    charge_regular = db.query(Charge).filter_by(account_id=account_regular.id).one()
    assert charge_privatized.amount == Decimal("500.00")
    assert charge_regular.amount == Decimal("500.00")
