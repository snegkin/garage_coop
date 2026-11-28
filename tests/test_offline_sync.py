"""Записи правления без сети (app/offline_sync.py): приём, внесение, разбор."""
import datetime as dt
import io
import json
from decimal import Decimal

from app.models import (
    ControlMeter, ControlMeterReading, ElectricityMeter, ElectricityReading, FieldEntry, FeeType,
    GarageContact, GarageOwnership, MemberAccount, MeterPhoto, Payment, Person, Phone, RoleEnum,
)
from tests.conftest import login, make_garage, make_ownership, make_person, make_user

TODAY = dt.date.today()


def _entry(kind, payload, uid=None, captured=None):
    return {"uuid": uid or f"{kind}-{json.dumps(payload, sort_keys=True).__hash__() & 0xffffff:x}", "kind": kind,
            "captured_at": (captured or dt.datetime.now()).isoformat(timespec="seconds"), "payload": payload}


def _sync(client, entries, files=None):
    data = {"entries": json.dumps(entries, ensure_ascii=False)}
    for name, content in (files or {}).items():
        data[name] = (io.BytesIO(content), "photo.jpg")
    resp = client.post("/offline/sync", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {r["uuid"]: r for r in resp.get_json()["results"]}


def _setup(db, role=RoleEnum.BOARD):
    owner = make_person(db, "Петров Пётр Петрович")
    db.add(Phone(person_id=owner.id, number="+7 900 000-00-01"))
    garage = make_garage(db, "7")
    make_ownership(db, garage, owner)
    meter = ElectricityMeter(garage_id=garage.id, meter_number="M-100", installed_date=TODAY - dt.timedelta(days=400),
                             initial_reading=Decimal("100"))
    db.add(meter)
    db.flush()
    db.add(ElectricityReading(meter_id=meter.id, reading=Decimal("500"), reading_date=TODAY - dt.timedelta(days=40)))
    user = make_user(db, "field", "pw", role)
    db.commit()
    return garage, owner, meter, user


def test_sync_requires_board(client, db):
    assert client.post("/offline/sync", data={"entries": "[]"}).status_code == 401
    make_user(db, "member", "pw", RoleEnum.MEMBER)
    db.commit()
    login(client, "member", "pw")
    assert client.post("/offline/sync", data={"entries": "[]"}).status_code == 403
    assert client.get("/offline/csrf").status_code == 200


def test_reading_applied_once_with_photo(client, db):
    garage, _owner, meter, _user = _setup(db)
    login(client, "field", "pw")
    day = (TODAY - dt.timedelta(days=2)).isoformat()
    e = _entry("reading", {"garage_id": garage.id, "meter_number": "M-100", "value": "620", "date": day}, uid="r1")
    res = _sync(client, [e], files={"photo_r1": b"\xff\xd8fakejpeg"})
    assert res["r1"]["status"] == "applied", res
    readings = db.query(ElectricityReading).filter_by(meter_id=meter.id).order_by(ElectricityReading.id).all()
    assert readings[-1].reading == Decimal("620") and readings[-1].reading_date.isoformat() == day
    assert "без сети" in readings[-1].comment
    photo = db.query(MeterPhoto).one()
    assert photo.reading_id == readings[-1].id
    # Повтор той же записи — не второй раз.
    again = _sync(client, [e], files={"photo_r1": b"\xff\xd8fakejpeg"})
    assert again["r1"]["status"] == "applied"
    assert db.query(ElectricityReading).filter_by(meter_id=meter.id).count() == 2
    assert db.query(FieldEntry).count() == 1


def test_reading_on_replaced_meter_waits_then_meter_entry_fixes(client, db):
    garage, _owner, _meter, _user = _setup(db)
    login(client, "field", "pw")
    wrong = _entry("reading", {"garage_id": garage.id, "meter_number": "NEW-1", "value": "15", "date": TODAY.isoformat()}, uid="r-new")
    res = _sync(client, [wrong])
    assert res["r-new"]["status"] == "review"
    assert "NEW-1" in res["r-new"]["message"]

    # В поле сначала внесли новый счётчик, потом показания — в одной пачке.
    meter_e = _entry("meter", {"garage_id": garage.id, "meter_number": "NEW-2", "initial_reading": "3",
                               "installed_date": TODAY.isoformat()}, uid="m-2")
    read_e = _entry("reading", {"garage_id": garage.id, "meter_number": "NEW-2", "value": "9", "date": TODAY.isoformat()}, uid="r-2")
    res = _sync(client, [meter_e, read_e])
    assert res["m-2"]["status"] == "applied" and res["r-2"]["status"] == "applied", res
    new_meter = db.query(ElectricityMeter).filter_by(meter_number="NEW-2").one()
    assert db.query(ElectricityReading).filter_by(meter_id=new_meter.id).one().reading == Decimal("9")


def test_bad_reading_goes_to_review_and_can_be_rejected(client, db):
    garage, *_ = _setup(db)
    login(client, "field", "pw")
    res = _sync(client, [_entry("reading", {"garage_id": garage.id, "meter_number": "M-100", "value": "400",
                                            "date": TODAY.isoformat()}, uid="low")])
    assert res["low"]["status"] == "review"
    assert "меньше предыдущих" in res["low"]["message"]
    entry = db.query(FieldEntry).filter_by(uuid="low").one()
    page = client.get("/offline/entries").get_data(as_text=True)
    assert "меньше предыдущих" in page
    client.post(f"/offline/entries/{entry.id}/reject", data={"reason": "ошибся цифрой"})
    db.refresh(entry)
    assert entry.status == "rejected" and "ошибся цифрой" in entry.message


def test_control_reading(client, db):
    _setup(db)
    node = ControlMeter(name="Ряд 2")
    db.add(node)
    db.flush()
    db.add(ControlMeterReading(control_meter_id=node.id, reading=Decimal("1000"), reading_date=TODAY - dt.timedelta(days=30)))
    db.commit()
    login(client, "field", "pw")
    res = _sync(client, [
        _entry("control_reading", {"control_meter_id": node.id, "value": "1100", "date": TODAY.isoformat()}, uid="c1"),
        _entry("control_reading", {"control_meter_id": node.id, "value": "900", "date": TODAY.isoformat()}, uid="c2"),
    ])
    assert res["c1"]["status"] == "applied" and res["c2"]["status"] == "review"
    assert db.query(ControlMeterReading).filter_by(control_meter_id=node.id).count() == 2


def test_payment_by_board_waits_for_chairman(client, db):
    garage, owner, _meter, _user = _setup(db)
    make_user(db, "chair", "pw", RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "field", "pw")
    res = _sync(client, [_entry("payment", {"target": "electricity", "garage_id": garage.id, "amount": "1500",
                                            "date": TODAY.isoformat(), "payer_person_id": owner.id,
                                            "receipt": "5-0001"}, uid="p1")])
    assert res["p1"]["status"] == "pending"
    assert db.query(Payment).count() == 0
    entry = db.query(FieldEntry).filter_by(uuid="p1").one()
    assert entry.receipt_number == "5-0001"
    # Рядовой член правления провести не может.
    assert client.post(f"/offline/entries/{entry.id}/retry").status_code == 403

    client.get("/auth/logout")
    login(client, "chair", "pw")
    html = client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "Записи без сети ждут решения" in html
    client.post(f"/offline/entries/{entry.id}/retry")
    db.refresh(entry)
    assert entry.status == "applied"
    payment = db.query(Payment).one()
    assert payment.amount == Decimal("1500") and payment.garage_id == garage.id
    assert "квитанция №5-0001" in payment.comment and "field" in payment.comment


def test_payment_by_chairman_to_member_account(client, db):
    _garage, owner, _meter, _user = _setup(db, role=RoleEnum.CHAIRMAN)
    fee = FeeType(code="membership", name="Членский взнос")
    db.add(fee)
    db.flush()
    account = MemberAccount(person_id=owner.id, fee_type_id=fee.id, account_number="7-01")
    db.add(account)
    db.commit()
    login(client, "field", "pw")
    res = _sync(client, [_entry("payment", {"target": "account", "account_id": account.id, "amount": "2000",
                                            "date": TODAY.isoformat(), "receipt": "3-0007"}, uid="p2")])
    assert res["p2"]["status"] == "applied", res
    payment = db.query(Payment).one()
    assert payment.account_id == account.id and payment.comment.startswith("Л/С 7-01: Наличные, квитанция №3-0007")
    data = client.get("/offline/snapshot.json").get_json()
    assert data["user"]["receipt_seq"] == 7


def test_person_create_with_garage_link(client, db):
    garage, owner, *_ = _setup(db)
    free = make_garage(db, "8")
    db.commit()
    login(client, "field", "pw")
    res = _sync(client, [
        _entry("person_create", {"fields": {"full_name": "Сидоров Сидор", "email": "s@example.org",
                                            "passport_number": "123456"},
                                 "phones": ["+7 911 111-11-11"],
                                 "link": {"garage_id": free.id, "role": "owner"}}, uid="n1"),
        _entry("person_create", {"fields": {"full_name": "Сидорова Анна"}, "phones": [],
                                 "link": {"garage_id": garage.id, "role": "contact", "relation": "дочь"}}, uid="n2"),
        # Гараж уже с собственником — продажа оформляется на сайте.
        _entry("person_create", {"fields": {"full_name": "Покупатель Новый"}, "phones": [],
                                 "link": {"garage_id": garage.id, "role": "replace"}}, uid="n3"),
    ])
    assert res["n1"]["status"] == "applied" and res["n2"]["status"] == "applied", res
    sid = db.query(Person).filter_by(full_name="Сидоров Сидор").one()
    assert sid.passport_number == "123456" and [p.number for p in sid.phones] == ["+7 911 111-11-11"]
    assert db.query(GarageOwnership).filter_by(garage_id=free.id, person_id=sid.id).count() == 1
    assert db.query(GarageContact).filter_by(garage_id=garage.id).one().relation == "дочь"
    assert res["n3"]["status"] == "review"
    buyer = db.query(Person).filter_by(full_name="Покупатель Новый").one()  # человек внесён, ждёт смены собственника
    assert db.query(GarageOwnership).filter_by(person_id=buyer.id).count() == 0
    entry = db.query(FieldEntry).filter_by(uuid="n3").one()
    assert entry.entity_id == buyer.id
    # «Повторить» не создаёт человека второй раз.
    client.post(f"/offline/entries/{entry.id}/retry")
    assert db.query(Person).filter_by(full_name="Покупатель Новый").count() == 1


def test_person_create_duplicate_name(client, db):
    _setup(db)
    login(client, "field", "pw")
    res = _sync(client, [_entry("person_create", {"fields": {"full_name": "петров пётр петрович"}, "phones": []}, uid="d1")])
    assert res["d1"]["status"] == "review"
    entry = db.query(FieldEntry).filter_by(uuid="d1").one()
    client.post(f"/offline/entries/{entry.id}/force")
    db.refresh(entry)
    assert entry.status == "applied"
    assert db.query(Person).filter(Person.full_name.in_(["Петров Пётр Петрович", "петров пётр петрович"])).count() == 2


def test_person_edit_and_conflict(client, db):
    _garage, owner, *_ = _setup(db)
    login(client, "field", "pw")
    res = _sync(client, [_entry("person_edit", {"person_id": owner.id, "changes": {
        "email": [None, "p@example.org"], "phones": [["+7 900 000-00-01"], ["+7 900 000-00-01", "+7 900 000-00-02"]],
    }}, uid="e1")])
    assert res["e1"]["status"] == "applied", res
    db.refresh(owner)
    assert owner.email == "p@example.org" and len(owner.phones) == 2

    # Кто-то на сайте уже поменял почту — правка с телефона ждёт решения.
    owner.email = "site@example.org"
    db.commit()
    res = _sync(client, [_entry("person_edit", {"person_id": owner.id, "changes": {
        "email": ["p@example.org", "field@example.org"]}}, uid="e2")])
    assert res["e2"]["status"] == "review" and "site@example.org" in res["e2"]["message"]
    db.refresh(owner)
    assert owner.email == "site@example.org"
    entry = db.query(FieldEntry).filter_by(uuid="e2").one()
    client.post(f"/offline/entries/{entry.id}/force")
    db.refresh(owner)
    assert owner.email == "field@example.org"


def test_link_existing_person_as_coowner(client, db):
    garage, _owner, *_ = _setup(db)
    other = make_person(db, "Совладелец Иван")
    db.commit()
    login(client, "field", "pw")
    res = _sync(client, [_entry("link", {"person_id": other.id, "link": {"garage_id": garage.id, "role": "coowner", "share": "0.5"}}, uid="l1")])
    assert res["l1"]["status"] == "applied", res
    assert db.query(GarageOwnership).filter_by(garage_id=garage.id, person_id=other.id).one().share == Decimal("0.5")


def test_sender_differs_from_author(client, db):
    garage, owner, _meter, _user = _setup(db, role=RoleEnum.CHAIRMAN)
    login(client, "field", "pw")
    _sync(client, [_entry("payment", {"target": "electricity", "garage_id": garage.id, "amount": "100",
                                      "date": TODAY.isoformat(), "receipt": "9-0001", "captured_by": "ivanov"}, uid="pp")])
    assert "принял ivanov (отправил field) без сети" in db.query(Payment).one().comment
