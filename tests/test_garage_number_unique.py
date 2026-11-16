"""Номер гаража уникален: занятый номер при создании/правке гаража — флеш
с сообщением, а не 500 на commit (так было на сервере 15.11.2026:
/garages/105/edit, номер «9м»)."""
from app.models import RoleEnum, Garage

from tests.conftest import make_garage, make_user, login


def _login_board(db, client):
    make_user(db, "board_num", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_num", "pass12345")


def test_edit_to_taken_number_is_rejected(db, client):
    make_garage(db, number="9м")
    garage = make_garage(db, number="105")
    _login_board(db, client)

    resp = client.post(f"/garages/{garage.id}/edit", data={
        "number": "9м", "area_sqm": "18", "coefficient": "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "Гараж №9м уже есть" in resp.get_data(as_text=True)
    db.expire_all()
    assert db.get(Garage, garage.id).number == "105"


def test_edit_keeping_own_number_works(db, client):
    garage = make_garage(db, number="106")
    _login_board(db, client)
    resp = client.post(f"/garages/{garage.id}/edit", data={"number": "106", "area_sqm": "20", "coefficient": "1"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/garages/{garage.id}")


def test_create_with_taken_number_is_rejected(db, client):
    make_garage(db, number="9м")
    _login_board(db, client)
    resp = client.post("/garages/new", data={"number": "9м", "area_sqm": "18", "coefficient": "1"}, follow_redirects=True)
    assert resp.status_code == 200
    assert "Гараж №9м уже есть" in resp.get_data(as_text=True)
    assert db.query(Garage).filter_by(number="9м").count() == 1
