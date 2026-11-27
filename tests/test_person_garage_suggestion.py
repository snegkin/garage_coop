"""
Номер гаража в имени или комментарии человека («Назарова (гараж 232)»,
«Макаров (гаражи 229, 230)», «Илья (141)») — карточка предлагает добавить
его в этот гараж: собственником, если у гаража собственника нет, иначе
лицом для связи. После добавления — возврат на карточку человека.
"""
from app import database
from app.models import RoleEnum, GarageOwnership, GarageContact
from app.persons import mentioned_garage_numbers

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_mentioned_garage_numbers():
    assert mentioned_garage_numbers("Назарова (гараж 232)") == ["232"]
    assert mentioned_garage_numbers("Макаров (гаражи 229, 230)") == ["229", "230"]
    assert mentioned_garage_numbers("Илья (141)") == ["141"]
    assert mentioned_garage_numbers(None, "гаражи 125, 126 и 183. Один из 125/126 продан") == ["125", "126", "183"]
    assert mentioned_garage_numbers("Гараж №179А") == ["179а"]
    assert mentioned_garage_numbers("Иванов Иван", "Прежний тел. +7 920 107-11-10") == []


def _board(db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")


def test_suggest_owner_for_garage_without_owners(app, db, client):
    garage = make_garage(db, "232")
    person = make_person(db, full_name="Назарова (гараж 232)")
    db.commit()
    _board(db, client)

    page = client.get(f"/persons/{person.id}").get_data(as_text=True)
    assert f"garageSuggestion{garage.id}" in page and "Собственником" in page

    resp = client.post(f"/garages/{garage.id}/owners/add",
                       data={"person_id": person.id, "share": "1", "return_to": "person"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/persons/{person.id}")
    assert database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id, person_id=person.id).count() == 1

    page = client.get(f"/persons/{person.id}").get_data(as_text=True)
    assert f"garageSuggestion{garage.id}" not in page


def test_suggest_only_contact_when_garage_has_owner(app, db, client):
    garage = make_garage(db, "232")
    owner = make_person(db, full_name="Назаров (гараж 232)")
    make_ownership(db, garage, owner)
    person = make_person(db, full_name="Назарова", comment="Жена, гараж 232")
    db.commit()
    _board(db, client)

    page = client.get(f"/persons/{person.id}").get_data(as_text=True)
    block = page.split(f"garageSuggestion{garage.id}", 1)[1].split("</div>\n      </div>", 1)[0]
    assert "Собственником" not in block and "Лицом для связи" in block and "Назаров (гараж 232)" in block

    resp = client.post(f"/garages/{garage.id}/contacts/add", data={"person_id": person.id, "return_to": "person"})
    assert resp.headers["Location"].endswith(f"/persons/{person.id}")
    assert database.db_session.query(GarageContact).filter_by(garage_id=garage.id, person_id=person.id).count() == 1
    assert f"garageSuggestion{garage.id}" not in client.get(f"/persons/{person.id}").get_data(as_text=True)


def test_no_suggestion_for_missing_garage_or_archived(app, db, client):
    garage = make_garage(db, "232")
    ghost = make_person(db, full_name="Пименов (гараж 196)")
    archived = make_person(db, full_name="Лидия (гараж 232)")
    archived.is_archived = True
    db.commit()
    _board(db, client)
    assert "garageSuggestion" not in client.get(f"/persons/{ghost.id}").get_data(as_text=True)
    assert f"garageSuggestion{garage.id}" not in client.get(f"/persons/{archived.id}").get_data(as_text=True)
