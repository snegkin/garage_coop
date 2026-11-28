"""Оффлайн-режим правления (app/offline.py): слепок базы, оболочка, service worker."""
import datetime as dt
from decimal import Decimal

from app.models import (
    Charge, ControlMeter, ControlMeterReading, ElectricityMeter, ElectricityReading, FeeType,
    GarageContact, MemberAccount, Payment, Phone, RoleEnum,
)
from tests.conftest import login, make_garage, make_ownership, make_person, make_user


def _seed(db):
    owner = make_person(db, "Иванов Иван Иванович")
    db.add(Phone(person_id=owner.id, number="+7 900 111-22-33", label="мобильный"))
    contact = make_person(db, "Иванова Мария")
    stranger = make_person(db, "Посторонний Человек")
    garage = make_garage(db, "15")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact.id, relation="супруга"))

    meter = ElectricityMeter(garage_id=garage.id, meter_number="0001234", installed_date=dt.date(2020, 1, 1))
    old_meter = ElectricityMeter(garage_id=garage.id, meter_number="OLD", installed_date=dt.date(2010, 1, 1))
    db.add_all([meter, old_meter])
    db.flush()
    db.add_all([
        ElectricityReading(meter_id=meter.id, reading=Decimal("1200.00"), reading_date=dt.date(2026, 9, 1)),
        ElectricityReading(meter_id=meter.id, reading=Decimal("1350.50"), reading_date=dt.date(2026, 10, 1)),
        ElectricityReading(meter_id=old_meter.id, reading=Decimal("99999"), reading_date=dt.date(2026, 11, 1)),
    ])
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00")))
    db.add(Payment(garage_id=garage.id, date=dt.date(2026, 10, 5), amount=Decimal("400.00")))

    fee = FeeType(code="membership", name="Членский взнос")
    penalty = FeeType(code="membership_penalty", name="Пеня по членскому", is_penalty=True)
    db.add_all([fee, penalty])
    db.flush()
    acc = MemberAccount(person_id=owner.id, garage_id=garage.id, fee_type_id=fee.id, account_number="15-01")
    pen = MemberAccount(person_id=owner.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="15-02")
    zero = MemberAccount(person_id=stranger.id, garage_id=None, fee_type_id=fee.id, account_number="99-01")
    db.add_all([acc, pen, zero])
    db.flush()
    db.add(Charge(account_id=acc.id, year=2026, amount=Decimal("3000.00")))
    db.add(Charge(account_id=pen.id, year=2026, amount=Decimal("50.00")))

    root = ControlMeter(name="Ряд 1")
    db.add(root)
    db.flush()
    db.add(ControlMeterReading(control_meter_id=root.id, reading=Decimal("500"), reading_date=dt.date(2026, 11, 1)))
    db.commit()
    return garage, owner


def test_snapshot_requires_board(client, db):
    resp = client.get("/offline/snapshot.json")
    assert resp.status_code == 401
    assert resp.is_json

    make_user(db, "member", "pw", RoleEnum.MEMBER)
    db.commit()
    login(client, "member", "pw")
    resp = client.get("/offline/snapshot.json")
    assert resp.status_code == 403


def test_snapshot_content_for_board(client, db):
    garage, owner = _seed(db)
    make_user(db, "board", "pw", RoleEnum.BOARD)
    db.commit()
    login(client, "board", "pw")

    resp = client.get("/offline/snapshot.json")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()

    g = next(x for x in data["garages"] if x["number"] == "15")
    assert g["electricity_balance"] == "-600.00"
    assert g["meter"] == {"number": "0001234", "last_reading": "1350.5", "last_date": "2026-10-01"}
    assert g["owners"] == [{"person_id": owner.id, "share": "1"}]
    assert g["contacts"][0]["relation"] == "супруга"

    names = {p["name"] for p in data["persons"]}
    assert names == {"Иванов Иван Иванович", "Иванова Мария"}  # без счетов и гаражей — не нужен
    person = next(p for p in data["persons"] if p["id"] == owner.id)
    assert person["phones"] == [{"number": "+7 900 111-22-33", "label": "мобильный"}]
    assert "passport_number" not in person

    # Пеня рядовому члену правления не видна — как на сайте.
    assert [a["balance"] for a in data["accounts"]] == ["-3000.00"]
    assert all(not f["penalty"] for f in data["fee_types"])

    assert data["control_meters"] == [{
        "id": data["control_meters"][0]["id"], "name": "Ряд 1", "parent_id": None, "parent_garage": None,
        "comment": None, "last_reading": "500", "last_date": "2026-11-01",
    }]


def test_snapshot_penalty_for_chairman(client, db):
    _seed(db)
    make_user(db, "chair", "pw", RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pw")
    data = client.get("/offline/snapshot.json").get_json()
    assert sorted(a["balance"] for a in data["accounts"]) == ["-3000.00", "-50.00"]


def test_snapshot_etag_not_modified(client, db):
    _seed(db)
    make_user(db, "board", "pw", RoleEnum.BOARD)
    db.commit()
    login(client, "board", "pw")
    first = client.get("/offline/snapshot.json")
    etag = first.headers["ETag"]
    again = client.get("/offline/snapshot.json", headers={"If-None-Match": etag})
    assert again.status_code == 304

    garage = db.query(ElectricityMeter).filter_by(meter_number="0001234").one().garage
    db.add(Payment(garage_id=garage.id, date=dt.date(2026, 11, 20), amount=Decimal("600.00")))
    db.commit()
    changed = client.get("/offline/snapshot.json", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["ETag"] != etag


def test_shell_is_public_and_has_no_data(client, db):
    _seed(db)
    resp = client.get("/offline/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/static/offline/store.js" in html
    assert "Иванов" not in html


def test_service_worker_precaches_offline_page(client):
    js = client.get("/service-worker.js").get_data(as_text=True)
    assert "addEventListener('push'" in js
    assert "var OFFLINE_URL = \"/offline/\"" in js
    assert "/static/offline/app.js" in js
    assert "bootstrap-icons.woff2" in js
    assert "coop-offline-" in js


def test_board_pages_register_worker_and_refresh(client, db):
    make_user(db, "board", "pw", RoleEnum.BOARD)
    make_user(db, "member", "pw", RoleEnum.MEMBER)
    db.commit()
    login(client, "board", "pw")
    html = client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "coopOffline.refresh()" in html
    assert "/offline/" in html  # пункт меню

    client.get("/auth/logout")
    login(client, "member", "pw")
    html = client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "coopOffline.refresh()" not in html


def test_logout_wipes_offline_data(client, db):
    make_user(db, "board", "pw", RoleEnum.BOARD)
    db.commit()
    login(client, "board", "pw")
    resp = client.get("/auth/logout", follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "coopOffline.wipe()" in html
    # Один раз — флаг одноразовый.
    assert "coopOffline.wipe()" not in client.get("/auth/login").get_data(as_text=True)


def test_snapshot_map_and_wiki(client, db):
    import json as _json
    from app.models import GarageMapObject, GarageMapObjectPhoto, GarageMapShape, GarageMapSettings, WikiAttachment, WikiPage

    garage, _owner = _seed(db)
    db.add(GarageMapSettings(id=1, width=800, height=600, background_file="map/bg.jpg", background_opacity=40))
    db.add(GarageMapShape(kind="garage", garage_id=garage.id, points=_json.dumps([[0, 0], [10, 0], [10, 10]])))
    db.add(GarageMapShape(kind="cable", points=_json.dumps([[0, 20], [50, 20]]), phases="AB"))
    page = WikiPage(title="Щит ряда 1", slug="shchit-ryada-1", body="Схема: ![](/wiki/attachments/7/shema.png)\n\nПароль: ||secret||")
    db.add(page)
    db.flush()
    db.add(WikiAttachment(page_id=page.id, original_filename="фото щита.jpg", stored_filename="w/x.jpg", is_inline=False))
    db.add(WikiAttachment(page_id=page.id, original_filename="инструкция.pdf", stored_filename="w/i.pdf", is_inline=False))
    db.add(WikiAttachment(page_id=page.id, original_filename="router-backup.tar.gz", stored_filename="w/b.tgz", is_inline=False))
    panel = GarageMapObject(kind="panel", x=5, y=5, title="Щит 1", description="Автоматы слева", wiki_page_id=page.id,
                            ip_address="192.168.1.10", device_username="login-x7q", device_password_encrypted="PWD-ENC-x7q")
    db.add(panel)
    db.flush()
    db.add(GarageMapObjectPhoto(object_id=panel.id, file_path="map/p1.jpg", caption="внутри"))
    db.add(GarageMapObject(kind="lamp", x=1, y=1, uplink_id=panel.id))
    make_user(db, "board", "pw", RoleEnum.BOARD)
    db.commit()
    login(client, "board", "pw")

    resp = client.get("/offline/snapshot.json")
    raw = resp.get_data(as_text=True)
    data = resp.get_json()
    assert "login-x7q" not in raw and "PWD-ENC-x7q" not in raw  # логины и пароли устройств — никогда

    m = data["map"]
    assert (m["width"], m["height"], m["background_opacity"]) == (800, 600, 40)
    assert m["background"].startswith("/garages/map/background/file?v=")
    assert [s["kind"] for s in m["shapes"]] == ["garage", "cable"]
    assert m["shapes"][1]["phases"] == "AB"
    obj = next(o for o in m["objects"] if o["kind"] == "panel")
    assert obj["title"] == "Щит 1" and obj["ip_address"] == "192.168.1.10" and obj["wiki_page_id"] == page.id
    assert obj["photos"][0]["caption"] == "внутри"
    photo_url = obj["photos"][0]["url"]

    w = next(x for x in data["wiki"] if x["slug"] == "shchit-ryada-1")
    assert w["slug"] == "shchit-ryada-1"
    assert '<img' in w["html"] and "/wiki/attachments/7/shema.png" in w["html"]
    assert w["images"][0]["name"] == "фото щита.jpg"

    assert m["background"] in data["media"]
    assert photo_url in data["media"]
    assert "/wiki/attachments/7/shema.png" in data["media"]
    assert w["images"][0]["url"] in data["media"]
    files = {f["name"]: f for f in w["files"]}
    assert files["инструкция.pdf"]["offline"] and files["инструкция.pdf"]["url"] in data["media"]
    assert not files["router-backup.tar.gz"]["offline"]  # бэкапы настроек устройств — не на телефон
    assert files["router-backup.tar.gz"]["url"] not in data["media"]
    assert next(g for g in data["garages"] if g["id"] == garage.id)["land_privatized"] is False


def test_snapshot_without_map(client, db):
    make_user(db, "board", "pw", RoleEnum.BOARD)
    db.commit()
    login(client, "board", "pw")
    data = client.get("/offline/snapshot.json").get_json()
    assert data["map"] is None
    assert data["media"] == []  # служебные страницы вики из миграций — без картинок


def test_service_worker_serves_media_from_cache(client):
    js = client.get("/service-worker.js").get_data(as_text=True)
    assert "var MEDIA_CACHE = 'coop-media'" in js
    assert "/garages/map/object/photos/" in js and "/wiki/attachments/" in js
    assert "/static/offline/map.js" in js
