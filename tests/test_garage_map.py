"""Схема территории (app/garage_map.py): страница, карточка, сохранение правок, импорт раскладки."""
import datetime as dt
import io
import json
import os
from decimal import Decimal

from app.models import (
    ControlMeter, ControlMeterReading, DvrCamera, DvrRecorder, GarageMapObject, GarageMapObjectFile, GarageMapObjectPhoto, GarageMapSettings,
    GarageMapShape, PowerPhaseDevice, PowerPhaseReading, RoleEnum, WikiPage,
)

from tests.conftest import make_garage, make_person, make_ownership, make_user, login

SQUARE = [[0, 0], [10, 0], [10, 20], [0, 20]]


def _board(db, client):
    make_user(db, "map_board", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "map_board", "pass12345")


def _save(client, shapes, **extra):
    return client.post("/garages/map/save", json={"shapes": shapes, **extra})


def test_member_cannot_open_map(db, client):
    make_user(db, "map_member", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "map_member", "pass12345")
    assert client.get("/garages/map/").status_code == 302


def test_map_page_and_card(db, client):
    garage = make_garage(db, number="17а")
    make_ownership(db, garage, make_person(db, full_name="Схемов Бокс Боксович"))
    db.add(GarageMapShape(kind="garage", garage_id=garage.id, points=json.dumps(SQUARE)))
    _board(db, client)

    page = client.get("/garages/map/").get_data(as_text=True)
    assert "Схема территории" in page
    assert f'"garage_id": {garage.id}' in page
    assert "Схемов Бокс Боксович" in page  # владелец во всплывающей подписи

    card = client.get(f"/garages/map/card/{garage.id}").get_data(as_text=True)
    assert "Гараж №17а" in card
    assert "Схемов Бокс Боксович" in card


def test_save_creates_updates_and_deletes(db, client):
    g1 = make_garage(db, number="1")
    g2 = make_garage(db, number="2")
    keep = GarageMapShape(kind="garage", garage_id=g1.id, points=json.dumps(SQUARE))
    drop = GarageMapShape(kind="road", points=json.dumps(SQUARE))
    db.add_all([keep, drop])
    db.flush()
    _board(db, client)

    moved = [[x + 5, y] for x, y in SQUARE]
    resp = _save(client, [
        # гараж 1 переезжает на новый контур, старый контур получает гараж 2
        {"id": keep.id, "kind": "garage", "garage_id": g2.id, "label": "", "points": moved},
        {"id": None, "kind": "garage", "garage_id": g1.id, "label": "", "points": SQUARE},
        {"id": None, "kind": "garage", "garage_id": None, "label": "1к", "points": SQUARE},
    ], background_opacity=30)
    assert resp.get_json() == {"ok": True}

    db.expire_all()
    shapes = db.query(GarageMapShape).order_by(GarageMapShape.sort_order).all()
    assert [(s.garage_id, s.label) for s in shapes] == [(g2.id, None), (g1.id, None), (None, "1к")]
    assert shapes[0].id == keep.id and shapes[0].point_list() == moved
    assert db.get(GarageMapSettings, 1).background_opacity == 30


def test_save_rejects_same_garage_twice_and_bad_points(db, client):
    garage = make_garage(db, number="5")
    _board(db, client)
    item = {"id": None, "kind": "garage", "garage_id": garage.id, "label": "", "points": SQUARE}
    resp = _save(client, [item, dict(item)])
    assert resp.status_code == 400 and "двум контурам" in resp.get_json()["error"]

    resp = _save(client, [{"kind": "garage", "points": [[0, 0], [1, 1]]}])
    assert resp.status_code == 400
    assert db.query(GarageMapShape).count() == 0


def test_import_layout_matches_numbers(db, app):
    from scripts.import_garage_map import import_layout

    garage = make_garage(db, number="66А")
    layout = {"width": 100, "height": 200, "shapes": [
        {"kind": "road", "points": SQUARE},
        {"kind": "garage", "number": "66а", "points": SQUARE},
        {"kind": "garage", "number": "2к", "points": SQUARE},
    ]}
    total, unmatched = import_layout(layout, replace=False)
    assert total == 3 and unmatched == ["2к"]
    shapes = db.query(GarageMapShape).order_by(GarageMapShape.sort_order).all()
    assert shapes[1].garage_id == garage.id
    assert shapes[2].label == "2к" and shapes[2].garage_id is None
    assert db.get(GarageMapSettings, 1).height == 200


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return buf.getvalue()


def test_save_objects_create_update_delete(db, client):
    node = ControlMeter(name="Ряд 34–63")
    keep = GarageMapObject(kind="mark", x=1, y=2, title="старое")
    drop = GarageMapObject(kind="lamp", x=5, y=5)
    db.add_all([node, keep, drop])
    db.flush()
    _board(db, client)

    resp = _save(client, [], objects=[
        {"id": keep.id, "kind": "panel", "x": 10.04, "y": 20, "title": " Щит ряда ", "description": "Питает 34–63",
         "master": False, "control_meter_id": node.id},
        {"id": None, "kind": "meter", "x": 3, "y": 4, "title": "Ввод", "master": True, "control_meter_id": node.id},
        {"id": None, "kind": "lamp", "x": 7, "y": 8, "master": True, "control_meter_id": node.id},
    ])
    assert resp.get_json() == {"ok": True}

    db.expire_all()
    objs = db.query(GarageMapObject).order_by(GarageMapObject.id).all()
    assert [o.kind for o in objs] == ["panel", "meter", "lamp"]
    panel, meter, lamp = objs
    assert panel.id == keep.id and (panel.x, panel.y) == (10.0, 20.0)
    assert panel.title == "Щит ряда" and panel.description == "Питает 34–63" and panel.control_meter_id == node.id
    assert meter.master and meter.control_meter_id is None  # главный счётчик — без контрольного
    assert not lamp.master and lamp.control_meter_id is None  # у фонаря привязки нет


def test_save_without_objects_keeps_them_and_rejects_unknown_meter(db, client):
    db.add(GarageMapObject(kind="switch", x=1, y=1))
    _board(db, client)
    assert _save(client, []).get_json() == {"ok": True}
    assert db.query(GarageMapObject).count() == 1

    resp = _save(client, [], objects=[{"kind": "meter", "x": 1, "y": 1, "control_meter_id": 999}])
    assert resp.status_code == 400
    assert db.query(GarageMapObject).count() == 1


def test_object_card_and_photos(db, client, app):
    node = ControlMeter(name="Контроль ряда А")
    db.add(node)
    db.flush()
    db.add(ControlMeterReading(control_meter_id=node.id, reading=Decimal("123.40"), reading_date=dt.date(2026, 11, 1)))
    obj = GarageMapObject(kind="meter", x=1, y=1, title="Счётчик ряда", description="Ряд 1–33", control_meter_id=node.id)
    db.add(obj)
    db.flush()
    _board(db, client)

    card = client.get(f"/garages/map/object/{obj.id}").get_data(as_text=True)
    assert "Счётчик ряда" in card and "Ряд 1–33" in card and "Контроль ряда А" in card and "123,40" in card

    resp = client.post(f"/garages/map/object/{obj.id}/photos",
                       data={"file": (io.BytesIO(_png_bytes()), "щит.png"), "caption": "вид спереди"},
                       content_type="multipart/form-data")
    assert resp.get_json() == {"ok": True}
    photo = db.query(GarageMapObjectPhoto).one()
    path = os.path.join(app.config["UPLOAD_FOLDER"], photo.file_path)
    assert os.path.exists(path) and photo.caption == "вид спереди"
    assert client.get(f"/garages/map/object/photos/{photo.id}/file").status_code == 200

    bad = client.post(f"/garages/map/object/{obj.id}/photos",
                      data={"file": (io.BytesIO(b"x"), "a.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 400

    # удаление объекта при сохранении схемы уносит и файлы фото
    assert _save(client, [], objects=[]).get_json() == {"ok": True}
    assert db.query(GarageMapObjectPhoto).count() == 0 and not os.path.exists(path)


def test_save_network_devices_with_uplinks(db, client):
    wiki = WikiPage(title="Сеть кооператива", slug="set", body="")
    phase = PowerPhaseDevice(label="Фаза A", ewelink_device_id="1000abc")
    db.add_all([wiki, phase])
    db.flush()
    _board(db, client)

    resp = _save(client, [], objects=[
        {"kind": "wifi", "x": 1, "y": 1, "title": "GM-Voschod-Link1", "net_role": "bridge",
         "model": "Ubiquiti LiteBeam AC Gen2", "ip_address": "192.168.1.9/255.255.255.0", "wiki_page_id": wiki.id},
        {"kind": "wifi", "x": 2, "y": 2, "title": "Хотспот 26и", "net_role": "hotspot", "ip_address": "192.168.2.150/24",
         "mac": "f0-b4-29-ed-42-6c", "uplink_index": 0},
        {"kind": "meter", "x": 3, "y": 3, "title": "POWCT", "net_role": "router", "uplink_index": 1,
         "power_device_id": phase.id},
        {"kind": "lamp", "x": 4, "y": 4, "ip_address": "10.0.0.1", "uplink_index": 1},
    ])
    assert resp.get_json() == {"ok": True}
    db.expire_all()
    link, hotspot, powct, lamp = db.query(GarageMapObject).order_by(GarageMapObject.id).all()
    assert link.ip_address == "192.168.1.9/24" and link.net_role == "bridge" and link.wiki_page_id == wiki.id
    assert hotspot.mac == "F0:B4:29:ED:42:6C" and hotspot.uplink_id == link.id
    assert powct.uplink_id == hotspot.id and powct.net_role is None and powct.power_device_id == phase.id
    assert lamp.ip_address is None and lamp.uplink_id is None  # у фонаря сетевых полей нет

    # удаление устройства отвязывает подключённые к нему
    keep = [{"id": o.id, "kind": o.kind, "x": o.x, "y": o.y, "title": o.title, "net_role": o.net_role,
             "ip_address": o.ip_address, "uplink_index": None} for o in (hotspot, powct)]
    keep[1]["uplink_index"] = 0
    assert _save(client, [], objects=keep).get_json() == {"ok": True}
    db.expire_all()
    hotspot, powct = db.query(GarageMapObject).order_by(GarageMapObject.id).all()
    assert hotspot.uplink_id is None and powct.uplink_id == hotspot.id


def test_save_network_rejects_bad_ip_mac_and_loops(db, client):
    _board(db, client)
    bad_ip = _save(client, [], objects=[{"kind": "wifi", "x": 1, "y": 1, "title": "A", "ip_address": "192.168.2.300/24"}])
    assert bad_ip.status_code == 400 and "IP" in bad_ip.get_json()["error"]
    bad_mac = _save(client, [], objects=[{"kind": "wifi", "x": 1, "y": 1, "mac": "zz:11"}])
    assert bad_mac.status_code == 400
    loop = _save(client, [], objects=[
        {"kind": "wifi", "x": 1, "y": 1, "uplink_index": 1},
        {"kind": "wifi", "x": 1, "y": 1, "uplink_index": 0},
    ])
    assert loop.status_code == 400 and "кольцо" in loop.get_json()["error"]
    to_lamp = _save(client, [], objects=[{"kind": "lamp", "x": 1, "y": 1}, {"kind": "wifi", "x": 1, "y": 1, "uplink_index": 0}])
    assert to_lamp.status_code == 400
    assert db.query(GarageMapObject).count() == 0


def test_object_backup_files_and_network_card(db, client, app):
    phase = PowerPhaseDevice(label="Фаза B", ewelink_device_id="1000def")
    db.add(phase)
    db.flush()
    db.add(PowerPhaseReading(device_id=phase.id, power_w=Decimal("190.50"), voltage_v=Decimal("228.00"), is_online=True))
    up = GarageMapObject(kind="wifi", x=1, y=1, title="Link1", net_role="bridge", ip_address="192.168.2.1/24")
    db.add(up)
    db.flush()
    obj = GarageMapObject(kind="wifi", x=1, y=1, title="Хотспот 4", net_role="hotspot", model="Xiaomi MiWiFi Nano",
                          ip_address="192.168.2.151/24", mac="F0:B4:29:5D:79:16", uplink_id=up.id)
    meter = GarageMapObject(kind="meter", x=2, y=2, title="POWCT", power_device_id=phase.id, uplink_id=None)
    db.add_all([obj, meter])
    db.flush()
    _board(db, client)

    card = client.get(f"/garages/map/object/{obj.id}").get_data(as_text=True)
    assert "Раздача Wi-Fi" in card and "Xiaomi MiWiFi Nano" in card and "192.168.2.151/24" in card
    assert "F0:B4:29:5D:79:16" in card and "Link1" in card and "Бэкапы настроек" in card
    assert "Подключены" in client.get(f"/garages/map/object/{up.id}").get_data(as_text=True)
    assert "190,50" in client.get(f"/garages/map/object/{meter.id}").get_data(as_text=True)

    resp = client.post(f"/garages/map/object/{obj.id}/files",
                       data={"file": (io.BytesIO(b"config"), "backup_2026.bin")}, content_type="multipart/form-data")
    assert resp.get_json() == {"ok": True}
    item = db.query(GarageMapObjectFile).one()
    download = client.get(f"/garages/map/object/files/{item.id}/download")
    assert download.data == b"config" and "backup_2026.bin" in download.headers["Content-Disposition"]
    assert client.post(f"/garages/map/object/{obj.id}/files",
                       data={"file": (io.BytesIO(b"x"), "a.html")}, content_type="multipart/form-data").status_code == 400
    path = os.path.join(app.config["UPLOAD_FOLDER"], item.file_path)
    assert client.post(f"/garages/map/object/files/{item.id}/remove").get_json() == {"ok": True}
    assert db.query(GarageMapObjectFile).count() == 0 and not os.path.exists(path)


def test_dvr_and_cameras_on_map(db, client):
    recorder = DvrRecorder(name="voshod45-recorder", host="192.168.2.50", port=554)
    db.add(recorder)
    db.flush()
    camera = DvrCamera(recorder_id=recorder.id, label="cam1-1", channel=1)
    db.add(camera)
    db.flush()
    _board(db, client)

    resp = _save(client, [], objects=[
        {"kind": "dvr", "x": 1, "y": 1, "title": "Регистратор", "dvr_recorder_id": recorder.id,
         "ip_address": "192.168.2.50/24", "dvr_camera_id": camera.id},
        {"kind": "camera", "x": 2, "y": 2, "title": "cam1-1", "dvr_camera_id": camera.id, "uplink_index": 0,
         "ip_address": "192.168.2.71/24", "dvr_recorder_id": recorder.id},
    ])
    assert resp.get_json() == {"ok": True}
    dvr_obj, cam_obj = db.query(GarageMapObject).order_by(GarageMapObject.id).all()
    assert dvr_obj.dvr_recorder_id == recorder.id and dvr_obj.dvr_camera_id is None
    assert cam_obj.dvr_camera_id == camera.id and cam_obj.dvr_recorder_id is None and cam_obj.uplink_id == dvr_obj.id

    card = client.get(f"/garages/map/object/{dvr_obj.id}").get_data(as_text=True)
    assert "192.168.2.50:554" in card and "cam1-1" in card
    card = client.get(f"/garages/map/object/{cam_obj.id}").get_data(as_text=True)
    assert f"/surveillance/cameras/{camera.id}/snapshot" in card and "канал" in card

    bad = _save(client, [], objects=[{"kind": "camera", "x": 1, "y": 1, "dvr_camera_id": 999}])
    assert bad.status_code == 400


def test_network_switch_as_uplink(db, client):
    _board(db, client)
    resp = _save(client, [], objects=[
        {"kind": "wifi", "x": 1, "y": 1, "title": "Link1", "net_role": "bridge"},
        {"kind": "netswitch", "x": 2, "y": 2, "title": "Свитч гараж 4", "uplink_index": 0, "net_role": "router"},
        {"kind": "dvr", "x": 3, "y": 3, "uplink_index": 1},
        {"kind": "wifi", "x": 4, "y": 4, "net_role": "hotspot", "uplink_index": 1},
    ])
    assert resp.get_json() == {"ok": True}
    link, sw, dvr, hotspot = db.query(GarageMapObject).order_by(GarageMapObject.id).all()
    assert sw.kind == "netswitch" and sw.uplink_id == link.id and sw.net_role is None
    assert dvr.uplink_id == sw.id and hotspot.uplink_id == sw.id


def test_save_power_cables_with_phases(db, client):
    _board(db, client)
    line = [[0, 0], [0, 100], [20, 150]]
    resp = _save(client, [
        {"kind": "cable", "label": "ряд 34–63", "phases": "авс", "points": line},  # русские буквы тоже
        {"kind": "cable", "phases": "b", "points": [[0, 0], [5, 5]]},
        {"kind": "road", "phases": "ABC", "points": SQUARE},
    ])
    assert resp.get_json() == {"ok": True}
    three, one, road = db.query(GarageMapShape).order_by(GarageMapShape.sort_order).all()
    assert three.phases == "ABC" and three.label == "ряд 34–63" and three.point_list() == line
    assert one.phases == "B" and road.phases is None
    # у контура (не линии) двух точек мало
    assert _save(client, [{"kind": "garage", "points": [[0, 0], [5, 5]]}]).status_code == 400
