"""
Личный кабинет «Мой гараж» (/cabinet/garages) и доступ к странице гаража
(garages.detail) для людей, указанных лицом для связи (GarageContact) —
не обязательно собственников. Раньше GarageContact давал только запись в
списке контактов гаража, без какого-либо доступа для самого этого
человека; теперь у него те же права на гараж, что и у собственника (см.
permissions.is_owner_or_board), а на своей странице «Мой гараж» он видит
такие гаражи отдельным блоком с пометкой, чей это гараж (не «свой»).
"""
from app.models import RoleEnum, GarageContact

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_contact_without_ownership_can_open_garage_detail(db, client):
    owner = make_person(db, full_name="Владелец Гаражный")
    contact_person = make_person(db, full_name="Контактов Контакт Контактович")
    garage = make_garage(db, number="301")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="доверенное лицо"))
    make_user(db, "contact1", "pass12345", role=RoleEnum.MEMBER, person=contact_person)
    db.commit()
    login(client, "contact1", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200


def test_contact_can_add_photo_document_and_contact_actions(db, client):
    """is_owner_or_board гейтит не только просмотр, но и все действия
    управления гаражом (см. app/garages.py) — контакт должен пройти и их."""
    owner = make_person(db, full_name="Владелец Гаражный Второй")
    contact_person = make_person(db, full_name="Доверенных Доверенный Доверенович")
    garage = make_garage(db, number="302")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="сосед"))
    make_user(db, "contact2", "pass12345", role=RoleEnum.MEMBER, person=contact_person)
    db.commit()
    login(client, "contact2", "pass12345")

    resp = client.post(f"/garages/{garage.id}/electricity/reading/add", data={})
    # Форма без счётчика вернёт понятный флэш-редирект (302), а не 403 —
    # именно 403 означал бы, что прав на само действие не было вовсе.
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/garages/{garage.id}"


def test_unrelated_member_still_gets_403_on_garage_detail(db, client):
    """Расширение прав контактам не должно давать доступ посторонним —
    регресс на is_owner_or_board."""
    owner = make_person(db, full_name="Владелец Гаражный Третий")
    other = make_person(db, full_name="Посторонний Человекович")
    garage = make_garage(db, number="303")
    make_ownership(db, garage, owner)
    make_user(db, "outsider1", "pass12345", role=RoleEnum.MEMBER, person=other)
    db.commit()
    login(client, "outsider1", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Личный кабинет — «Мой гараж»
# ---------------------------------------------------------------------------

def test_cabinet_shows_contact_garage_with_owner_label_when_no_own_garages(db, client):
    owner = make_person(db, full_name="Иванов Иван Иванович")
    contact_person = make_person(db, full_name="Контактов Контакт Контактович")
    garage = make_garage(db, number="304")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="супруга"))
    make_user(db, "contact3", "pass12345", role=RoleEnum.MEMBER, person=contact_person)
    db.commit()
    login(client, "contact3", "pass12345")

    resp = client.get("/cabinet/garages")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "За вами не числится ни одного гаража." not in body
    assert "Гаражи, где вы указаны лицом для связи" in body
    assert "Иванов Иван Иванович" in body
    assert f"/garages/{garage.id}" in body


def test_cabinet_does_not_duplicate_garage_already_owned(db, client):
    """Если человек и собственник, и почему-то тоже вписан контактом того
    же гаража — карточка одна, в блоке собственных гаражей, без
    дублирования во втором блоке."""
    person = make_person(db, full_name="Двойников Двойник Двойникович")
    garage = make_garage(db, number="305")
    make_ownership(db, garage, person)
    db.add(GarageContact(garage_id=garage.id, person_id=person.id, relation="сам себе"))
    make_user(db, "dual1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "dual1", "pass12345")

    resp = client.get("/cabinet/garages")
    body = resp.get_data(as_text=True)
    assert "Гаражи, где вы указаны лицом для связи" not in body
    assert body.count(f'href="/garages/{garage.id}"') == 1


def test_cabinet_shows_no_garages_message_without_ownership_or_contacts(db, client):
    person = make_person(db, full_name="Никакой Никак Никакович")
    make_user(db, "nogarage1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "nogarage1", "pass12345")

    resp = client.get("/cabinet/garages")
    body = resp.get_data(as_text=True)
    assert "За вами не числится ни одного гаража." in body
    assert "Гаражи, где вы указаны лицом для связи" not in body
