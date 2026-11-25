"""Схема территории (app/garage_map.py): страница, карточка, сохранение правок, импорт раскладки."""
import json

from app.models import GarageMapSettings, GarageMapShape, RoleEnum

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
