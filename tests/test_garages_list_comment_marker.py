"""
Список гаражей (/garages/) — значок у номера гаража, если у него заполнен
комментарий (Garage.comment), чтобы такие гаражи было видно сразу, не
открывая карточку каждого.
"""
from app.models import RoleEnum, Garage

from tests.conftest import make_garage, make_user, login


def test_garage_with_comment_shows_marker(app, db, client):
    garage = make_garage(db, number="50", comment="Требует ремонта ворот")
    make_user(db, "board50", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board50", "pass12345")

    resp = client.get("/garages/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Требует ремонта ворот" in body  # title-подсказка со значком


def test_garage_without_comment_shows_no_marker(app, db, client):
    garage = make_garage(db, number="51")
    make_user(db, "board51", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board51", "pass12345")

    resp = client.get("/garages/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "✎" not in body
