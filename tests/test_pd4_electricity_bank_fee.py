"""
Комиссия банка (Cooperative.bank_fee_percent) добавляется к сумме
QR-кода/квитанции ПД-4 на оплату ЭЛЕКТРОЭНЕРГИИ (app/pd4.py:
_build_mixed_slips) — сам долг за электричество считается по факту
потребления, без комиссии (см. accounting.bank_fee_multiplier), комиссия
добавляется только в сумму К ОПЛАТЕ именно этим переводом, чтобы после
удержания банком своей доли на счёт кооператива поступил полный долг.

Взносы/налоги (MemberAccount) эту прибавку на этом шаге не получают —
если для конкретного вида взноса (земельный налог) комиссия нужна, она
уже заложена в само начисление при его расчёте (accounting.compute_land_tax).
"""
from decimal import Decimal

from app.accounting import bank_fee_multiplier
from app.models import (
    Cooperative, RoleEnum, Garage, GarageOwnership, PersonalAccount, Charge,
    MemberAccount, FeeType, PD4Document,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_coop(db, bank_fee_percent=Decimal("1.6")):
    coop = Cooperative(
        full_name="Тестовый кооператив", inn="1234567890", kpp="123456789", ogrn="1234567890123",
        bank_fee_percent=bank_fee_percent,
    )
    db.add(coop)
    db.flush()
    return coop


# ---------------------------------------------------------------------------
# accounting.bank_fee_multiplier — сам множитель
# ---------------------------------------------------------------------------

def test_bank_fee_multiplier_default_percent(db):
    coop = _make_coop(db, bank_fee_percent=Decimal("1.6"))
    assert bank_fee_multiplier(coop) == Decimal("1.016")


def test_bank_fee_multiplier_zero_percent(db):
    coop = _make_coop(db, bank_fee_percent=Decimal("0"))
    assert bank_fee_multiplier(coop) == Decimal("1")


def test_bank_fee_multiplier_unset_percent(db):
    coop = _make_coop(db, bank_fee_percent=None)
    assert bank_fee_multiplier(coop) == Decimal("1")


# ---------------------------------------------------------------------------
# Печать квитанции/QR на электроэнергию — комиссия прибавляется
# ---------------------------------------------------------------------------

def test_electricity_slip_amount_includes_bank_fee(db, client):
    _make_coop(db, bank_fee_percent=Decimal("1.6"))
    person = make_person(db, full_name="Электричество Тестович")
    garage = make_garage(db, number="401")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="40101"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00")))
    make_user(db, "elecowner1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "elecowner1", "pass12345")

    resp = client.get(f"/pd4/print?garage_id={garage.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "1016,00" in body  # fmt2 без разделителя тысяч, запятая — дробная часть (ru-локаль)

    doc = db.query(PD4Document).one()
    assert doc.amount == Decimal("1016.00")


def test_electricity_slip_qr_payload_sum_includes_bank_fee(db, client):
    _make_coop(db, bank_fee_percent=Decimal("1.6"))
    person = make_person(db, full_name="Электричество Кьюаров")
    garage = make_garage(db, number="402")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="40201"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("500.00")))
    make_user(db, "elecowner2", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "elecowner2", "pass12345")

    client.get(f"/pd4/print?garage_id={garage.id}")
    doc = db.query(PD4Document).one()
    # 500 * 1.016 = 508.00 -> 50800 копеек в поле Sum= QR-кода (СБП-формат ST00012)
    assert "Sum=50800" in doc.qr_payload


def test_electricity_slip_no_fee_when_bank_fee_percent_is_zero(db, client):
    _make_coop(db, bank_fee_percent=Decimal("0"))
    person = make_person(db, full_name="Электричество Нольевич")
    garage = make_garage(db, number="403")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="40301"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("777.00")))
    make_user(db, "elecowner3", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "elecowner3", "pass12345")

    client.get(f"/pd4/print?garage_id={garage.id}")
    doc = db.query(PD4Document).one()
    assert doc.amount == Decimal("777.00")


def test_member_dues_slip_amount_unaffected_by_bank_fee(db, client):
    """Взнос без баковской комиссии, заложенной в начисление (обычный
    членский взнос, не земельный налог) — печать квитанции НЕ добавляет
    комиссию поверх (в отличие от электроэнергии)."""
    _make_coop(db, bank_fee_percent=Decimal("1.6"))
    person = make_person(db, full_name="Взносов Взнос Взносович")
    garage = make_garage(db, number="404")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="membership", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="50404")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("1000.00")))
    make_user(db, "duesowner1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "duesowner1", "pass12345")

    client.get(f"/pd4/print?account_id={account.id}")
    doc = db.query(PD4Document).one()
    assert doc.amount == Decimal("1000.00")  # без комиссии — она сюда не добавляется
