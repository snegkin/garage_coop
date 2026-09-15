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
    assert updated_personal.account_number == f"{settings.type_code}{str(garage.id).zfill(settings.garage_digits)}{'0' * settings.owner_digits}"
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


def test_account_format_page_shows_all_types_but_regenerate_only_with_type_code(app, db, client):
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    target = _fee_type(db, code="target", name="Целевой взнос", type_code=None)
    make_user(db, "chair_fmt4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt4", "pass12345")
    resp = client.get("/finance/account-format")
    assert resp.status_code == 200
    # Оба вида показаны (названия/комментарии редактируются для всех) —
    # но кнопка "Переименовать по формату" — только там, где есть формула.
    assert f'name="scope" value="{land_tax.id}"'.encode() in resp.data
    assert f'name="scope" value="{target.id}"'.encode() not in resp.data


def test_electricity_fee_type_merged_into_single_row(app, db, client):
    """
    FeeType(code="electricity") — необязательная запись только ради
    названия в квитанциях (см. accounting.pd4_qr_payload_electricity), её
    type_code для нумерации счёта на электричество не используется (та
    идёт через PersonalAccount + AccountNumberSettings.type_code). Если
    такая запись заведена — её название/комментарий редактируются в ТОЙ
    ЖЕ строке "Электричество", а не отдельной второй строкой, и без
    отдельной (бесполезной для неё) кнопки "Переименовать" по fee_type_id.
    """
    elec = _fee_type(db, code="electricity", name="Электричество", type_code=None)
    make_user(db, "chair_fmt9", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt9", "pass12345")
    resp = client.get("/finance/account-format")
    assert resp.status_code == 200
    assert resp.data.count("Электричество".encode()) == 1  # не дублируется
    assert f'name="name_{elec.id}"'.encode() in resp.data  # название редактируемо
    assert f'name="type_code_{elec.id}"'.encode() not in resp.data  # свой code_type не задействован
    assert b'name="scope" value="electricity"' in resp.data
    assert f'name="scope" value="{elec.id}"'.encode() not in resp.data  # нет второй, бесполезной кнопки

    resp = client.post("/finance/account-format/fee-types", data={f"name_{elec.id}": "Электроэнергия"})
    assert resp.status_code == 302
    db.expire_all()
    assert db.get(FeeType, elec.id).name == "Электроэнергия"


def test_bulk_update_fee_type_names_and_comments(app, db, client):
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    membership = _fee_type(db, code="membership", name="Членский взнос", type_code="2")
    make_user(db, "chair_fmt6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt6", "pass12345")
    resp = client.post("/finance/account-format/fee-types", data={
        f"name_{land_tax.id}": "Земельный налог (ААА)",
        f"comment_{land_tax.id}": "по кадастровой стоимости",
        f"type_code_{land_tax.id}": "1",  # без изменений
        f"name_{membership.id}": "Членский взнос",  # без изменений
        f"comment_{membership.id}": "",
        f"type_code_{membership.id}": "5",  # изменён (не "9" — зарезервирован за электричеством)
    })
    assert resp.status_code == 302

    db.expire_all()
    updated_land = db.get(FeeType, land_tax.id)
    updated_member = db.get(FeeType, membership.id)
    assert updated_land.name == "Земельный налог (ААА)"
    assert updated_land.comment == "по кадастровой стоимости"
    assert updated_member.name == "Членский взнос"
    assert updated_member.type_code == "5"

    log = db.query(AuditLog).filter_by(action="fee_type.bulk_update").one()
    assert "2" in log.summary  # изменены оба (у земельного — название/комментарий, у членского — код)


def test_bulk_update_electricity_type_code(app, db, client):
    make_user(db, "chair_fmt8", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt8", "pass12345")
    settings = get_settings()
    assert settings.type_code == "9"  # дефолт — по прямой просьбе, счета на электричество последние в списке
    resp = client.post("/finance/account-format/fee-types", data={"electricity_type_code": "Э"})
    assert resp.status_code == 302

    db.expire_all()
    settings = get_settings()
    assert settings.type_code == "Э"


def test_bulk_update_skips_blank_name(app, db, client):
    """Пустое название не стирает существующее — пропускаем такой вид."""
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    make_user(db, "chair_fmt7", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt7", "pass12345")
    client.post("/finance/account-format/fee-types", data={f"name_{land_tax.id}": "   "})

    db.expire_all()
    assert db.get(FeeType, land_tax.id).name == "Земельный налог"


def test_regenerate_invalid_scope_404(app, db, client):
    make_user(db, "chair_fmt5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_fmt5", "pass12345")
    resp = client.post("/finance/account-format/regenerate", data={"scope": "not-a-number"})
    assert resp.status_code == 404


def test_bulk_update_rejects_type_code_reserved_by_electricity(app, db, client):
    """Код "9" (дефолт для электричества) занять видом взноса нельзя."""
    membership = _fee_type(db, code="membership", name="Членский взнос", type_code="2")
    make_user(db, "chair_fmt10", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt10", "pass12345")
    settings = get_settings()
    assert settings.type_code == "9"
    resp = client.post("/finance/account-format/fee-types", data={
        f"name_{membership.id}": "Членский взнос",
        f"type_code_{membership.id}": "9",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "зарезервирован".encode() in resp.data

    db.expire_all()
    assert db.get(FeeType, membership.id).type_code == "2"  # не изменился


def test_bulk_update_rejects_electricity_code_used_by_fee_type(app, db, client):
    """И наоборот: электричеству нельзя занять код, уже занятый видом взноса."""
    _fee_type(db, code="membership", name="Членский взнос", type_code="2")
    make_user(db, "chair_fmt11", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_fmt11", "pass12345")
    resp = client.post("/finance/account-format/fee-types", data={"electricity_type_code": "2"}, follow_redirects=True)
    assert resp.status_code == 200
    assert "зарезервирован".encode() in resp.data or "занят".encode() in resp.data

    db.expire_all()
    assert get_settings().type_code == "9"  # не изменился


def test_create_fee_type_rejects_reserved_type_code(app, db, client):
    make_user(db, "chair_fmt12", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_fmt12", "pass12345")
    assert get_settings().type_code == "9"

    resp = client.post("/finance/fee-types/new", data={
        "code": "some_fee", "name": "Некий взнос", "type_code": "9",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "зарезервирован".encode() in resp.data
    assert db.query(FeeType).filter_by(code="some_fee").first() is None


def test_backfill_creates_missing_accounts_for_existing_owners(app, db, client):
    """
    "Целевой взнос" и т.п. заведены ПОСЛЕ того, как гаражи/собственники уже
    были — счёт сам не появился (это заводится только при добавлении
    НОВОГО собственника, см. garages._ensure_member_accounts). Кнопка
    "Завести всем" — бэкофилл задним числом для уже существующих.
    """
    from app.accounting import member_account_number, get_settings

    p1 = make_person(db, full_name="Первый Собственников")
    p2 = make_person(db, full_name="Второй Собственников")
    g1 = make_garage(db, number="101")
    g2 = make_garage(db, number="102")
    make_ownership(db, g1, p1)
    make_ownership(db, g2, p2)
    target = _fee_type(db, code="target", name="Целевой взнос", type_code="4")
    make_user(db, "chair_bf1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_bf1", "pass12345")
    resp = client.post("/finance/account-format/backfill", data={"fee_type_id": str(target.id)})
    assert resp.status_code == 302

    db.expire_all()
    accounts = db.query(MemberAccount).filter_by(fee_type_id=target.id).all()
    assert len(accounts) == 2
    settings = get_settings()
    numbers = {a.account_number for a in accounts}
    assert member_account_number("4", g1.id, 0, False, settings) in numbers
    assert member_account_number("4", g2.id, 0, False, settings) in numbers

    log = db.query(AuditLog).filter_by(action="member_account.backfill").one()
    assert "Целевой взнос" in log.summary


def test_backfill_skips_existing_accounts(app, db, client):
    person = make_person(db, full_name="Уже Есть Счётович")
    garage = make_garage(db, number="103")
    make_ownership(db, garage, person)
    target = _fee_type(db, code="target", name="Целевой взнос", type_code="4")
    db.add(MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=target.id, account_number="EXISTING"))
    make_user(db, "chair_bf2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_bf2", "pass12345")
    resp = client.post("/finance/account-format/backfill", data={"fee_type_id": str(target.id)}, follow_redirects=True)
    assert resp.status_code == 200
    assert "уже есть".encode() in resp.data

    db.expire_all()
    accounts = db.query(MemberAccount).filter_by(fee_type_id=target.id).all()
    assert len(accounts) == 1  # не задвоили


def test_backfill_per_garage_false_one_account_per_person(app, db, client):
    """per_garage=False — один счёт на человека, а не на каждый его гараж."""
    person = make_person(db, full_name="Два Гаража Человекович")
    g1 = make_garage(db, number="104")
    g2 = make_garage(db, number="105")
    make_ownership(db, g1, person)
    make_ownership(db, g2, person)
    disputes = _fee_type(db, code="disputes2", name="Споры", type_code="5", per_garage=False)
    make_user(db, "chair_bf3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_bf3", "pass12345")
    resp = client.post("/finance/account-format/backfill", data={"fee_type_id": str(disputes.id)})
    assert resp.status_code == 302

    db.expire_all()
    accounts = db.query(MemberAccount).filter_by(fee_type_id=disputes.id).all()
    assert len(accounts) == 1
    assert accounts[0].garage_id is None


def test_backfill_requires_type_code(app, db, client):
    target = _fee_type(db, code="target", name="Целевой взнос", type_code=None)
    make_user(db, "chair_bf4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_bf4", "pass12345")
    resp = client.post("/finance/account-format/backfill", data={"fee_type_id": str(target.id)}, follow_redirects=True)
    assert resp.status_code == 200
    assert "нет кода счёта".encode() in resp.data
    assert db.query(MemberAccount).filter_by(fee_type_id=target.id).count() == 0


def test_backfill_button_shown_only_with_type_code(app, db, client):
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог", type_code="1")
    target = _fee_type(db, code="target", name="Целевой взнос", type_code=None)
    make_user(db, "chair_bf5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair_bf5", "pass12345")
    resp = client.get("/finance/account-format")
    assert resp.status_code == 200
    assert f'name="fee_type_id" value="{land_tax.id}"'.encode() in resp.data
    assert f'name="fee_type_id" value="{target.id}"'.encode() not in resp.data
