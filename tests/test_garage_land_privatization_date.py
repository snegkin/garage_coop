"""
Дата приватизации земельного участка (Garage.land_privatization_date) —
заполняется на форме создания/редактирования гаража вместе с остальными
полями приватизации (land_privatized/land_cadastral_number/
privatized_land_area), см. app/templates/garages/_fields.html.
"""
import datetime as dt

from app.models import RoleEnum, Garage

from tests.conftest import make_garage, make_user, login


def test_create_garage_saves_land_privatization_date(db, client):
    make_user(db, "board600", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board600", "pass12345")

    resp = client.post("/garages/new", data={
        "number": "301", "area_sqm": "18.5", "coefficient": "1",
        "land_privatized": "on", "land_cadastral_number": "77:01:0001001:999",
        "privatized_land_area": "18.5", "land_privatization_date": "2020-06-15",
    })
    assert resp.status_code == 302

    garage = db.query(Garage).filter_by(number="301").one()
    assert garage.land_privatization_date == dt.date(2020, 6, 15)


def test_edit_garage_updates_land_privatization_date(db, client):
    garage = make_garage(db, number="302", land_privatized=True)
    make_user(db, "board601", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board601", "pass12345")

    resp = client.post(f"/garages/{garage.id}/edit", data={
        "number": garage.number, "area_sqm": str(garage.area_sqm), "coefficient": "1",
        "land_privatized": "on", "land_privatization_date": "2021-09-01",
    })
    assert resp.status_code == 302

    db.refresh(garage)
    assert garage.land_privatization_date == dt.date(2021, 9, 1)


def test_garage_detail_shows_land_privatization_date(db, client):
    garage = make_garage(
        db, number="303", land_privatized=True,
        land_privatization_date=dt.date(2019, 3, 10),
    )
    make_user(db, "board602", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board602", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    body = resp.get_data(as_text=True)
    assert "10.03.2019" in body
