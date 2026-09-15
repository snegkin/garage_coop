"""
/finance/account-format/regenerate — переименовать по текущему формату
счета ОДНОГО вида (электричество или конкретный вид взноса), отдельно от
общего чекбокса "Пересчитать существующие" на форме настроек (тот сразу
все виды разом). Нужно, когда сам формат верный, но конкретный вид был
когда-то заведён не по формуле (ручной ввод/импорт).
"""
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, PersonalAccount, AuditLog
from app import database
from app.accounting import get_settings

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _fee_type(db, code="land_tax", name="Земельный налог", type_code="1", per_garage=True):
    ft = FeeType(code=code, name=name, type_code=type_code, is_penalty=False, per_garage=per_garage)
    db.add(ft)
    db.flush()
    return ft


def test_regenerate_single_fee_type_renames_only_that_type(app, db, client):
    person = make_person(db, full_name="Форматов Формат Форматович")
    garage = make_garage(db, number="95")
    make_ownership(db, garage, person)
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    membership = _fee_type(db, code="membership", name="Членский взнос", type_code="2")
    make_user(db, "chair_fmt1", "pass12345", role=RoleEnum.CHAIRMAN)

    # Заведены "не по формуле" — произвольные номера вручную.
    land_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="LT-OLD")
    member_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="M-OLD")
    db.add(land_account)
    db.add(member_account)
    db.commit()

    login(client, "chair_fmt1", "pass12345")
    resp = client.post("/finance/account-format/regenerate", data={"scope": str(land_tax.id)})
    assert resp.status_code == 302

    db.expire_all()
    updated_land = db.get(MemberAccount, land_account.id)
    updated_member = db.get(MemberAccount, member_account.id)
    settings = get_settings()
    assert updated_land.account_number == f"1{str(garage.id).zfill(settings.garage_digits)}0"
    assert updated_member.account_number == "M-OLD"  # другой вид — не тронут

    log = db.query(AuditLog).filter_by(action="account_format.regenerate_type").one()
    assert "Земельный налог" in log.summary


def test_regenerate_electricity_scope_does_not_touch_member_accounts(app, db, client):
    person = make_person(db, full_name="Электричество Тестович")
    garage = make_garage(db, number="96")
    make_ownership(db, garage, person)
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    member_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="M-OLD")
    personal_account = PersonalAccount(garage_id=garage.id, account_number="OLD-ELEC")
    db.add(member_account)
    db.add(personal_account)
    make_user(db, "chair_fmt2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt2", "pass12345")
    resp = client.post("/finance/account-format/regenerate", data={"scope": "electricity"})
    assert resp.status_code == 302

    db.expire_all()
    updated_personal = db.get(PersonalAccount, personal_account.id)
    updated_member = db.get(MemberAccount, member_account.id)
    settings = get_settings()
    assert updated_personal.account_number == f"{settings.electricity_prefix}{str(garage.id).zfill(settings.garage_digits)}{'0' * settings.owner_digits}"
    assert updated_member.account_number == "M-OLD"  # электричество — не задело взносы


def test_regenerate_reports_conflict(app, db, client):
    person1 = make_person(db, full_name="Первый Собственник")
    person2 = make_person(db, full_name="Второй Собственник")
    garage1 = make_garage(db, number="97")
    garage2 = make_garage(db, number="98")
    make_ownership(db, garage1, person1)
    make_ownership(db, garage2, person2)
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    settings = get_settings()
    # Целевой (правильный) номер для garage1 уже занят другим счётом garage2 — конфликт.
    from app.accounting import member_account_number
    target_number = member_account_number("1", garage1.id, 0, False, settings)
    conflicting = MemberAccount(person_id=person2.id, garage_id=garage2.id, fee_type_id=land_tax.id, account_number=target_number)
    to_fix = MemberAccount(person_id=person1.id, garage_id=garage1.id, fee_type_id=land_tax.id, account_number="WRONG")
    db.add(conflicting)
    db.add(to_fix)
    make_user(db, "chair_fmt3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt3", "pass12345")
    resp = client.post("/finance/account-format/regenerate", data={"scope": str(land_tax.id)}, follow_redirects=True)
    assert resp.status_code == 200
    assert "конфликт".encode() in resp.data or "Не удалось".encode() in resp.data

    db.expire_all()
    assert db.get(MemberAccount, to_fix.id).account_number == "WRONG"  # не переименован — конфликт


def test_account_format_page_lists_only_fee_types_with_type_code(app, db, client):
    _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    _fee_type(db, code="target", name="Целевой взнос", type_code=None)
    make_user(db, "chair_fmt4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt4", "pass12345")
    resp = client.get("/finance/account-format")
    assert resp.status_code == 200
    assert "Земельный налог".encode() in resp.data
    assert "Целевой взнос".encode() not in resp.data


def test_regenerate_invalid_scope_404(app, db, client):
    make_user(db, "chair_fmt5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_fmt5", "pass12345")
    resp = client.post("/finance/account-format/regenerate", data={"scope": "not-a-number"})
    assert resp.status_code == 404
