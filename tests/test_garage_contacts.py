"""
Лица для связи по гаражу (GarageContact) — редактирование записи (человек +
отношение) без удаления и повторного добавления, см. app/garages.py:
edit_contact. Права те же, что у add_contact/remove_contact: правление или
собственник этого гаража (is_owner_or_board).
"""
from app.models import RoleEnum, GarageContact

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_board_can_edit_contact_relation(db, client):
    owner = make_person(db, full_name="Владелец Гаражный")
    contact_person = make_person(db, full_name="Родственник Родственникович")
    garage = make_garage(db, number="201")
    make_ownership(db, garage, owner)
    contact = GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="сосед")
    db.add(contact)
    make_user(db, "board400", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board400", "pass12345")

    resp = client.post(f"/garages/{garage.id}/contacts/{contact.id}/edit", data={
        "person_id": str(contact_person.id), "relation": "супруга",
    })
    assert resp.status_code == 302

    db.refresh(contact)
    assert contact.relation == "супруга"
    assert contact.person_id == contact_person.id


def test_edit_contact_can_change_person(db, client):
    owner = make_person(db, full_name="Владелец Второй")
    contact_person_a = make_person(db, full_name="Первый Контактович")
    contact_person_b = make_person(db, full_name="Второй Контактович")
    garage = make_garage(db, number="202")
    make_ownership(db, garage, owner)
    contact = GarageContact(garage_id=garage.id, person_id=contact_person_a.id, relation="доверенное лицо")
    db.add(contact)
    make_user(db, "board401", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board401", "pass12345")

    resp = client.post(f"/garages/{garage.id}/contacts/{contact.id}/edit", data={
        "person_id": str(contact_person_b.id), "relation": "доверенное лицо",
    })
    assert resp.status_code == 302

    db.refresh(contact)
    assert contact.person_id == contact_person_b.id


def test_owner_can_edit_own_garage_contact(db, client):
    owner = make_person(db, full_name="Собственник Собственникович")
    contact_person = make_person(db, full_name="Контакт Контактович")
    garage = make_garage(db, number="203")
    make_ownership(db, garage, owner)
    contact = GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="сосед")
    db.add(contact)
    make_user(db, "owner400", "pass12345", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    login(client, "owner400", "pass12345")

    resp = client.post(f"/garages/{garage.id}/contacts/{contact.id}/edit", data={
        "person_id": str(contact_person.id), "relation": "друг",
    })
    assert resp.status_code == 302

    db.refresh(contact)
    assert contact.relation == "друг"


def test_unrelated_member_cannot_edit_garage_contact(db, client):
    owner = make_person(db, full_name="Владелец Третий")
    other = make_person(db, full_name="Посторонний Посторонникович")
    contact_person = make_person(db, full_name="Контакт Третьякович")
    garage = make_garage(db, number="204")
    make_ownership(db, garage, owner)
    contact = GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="сосед")
    db.add(contact)
    make_user(db, "member400", "pass12345", role=RoleEnum.MEMBER, person=other)
    db.commit()
    login(client, "member400", "pass12345")

    resp = client.post(f"/garages/{garage.id}/contacts/{contact.id}/edit", data={
        "person_id": str(contact_person.id), "relation": "друг",
    })
    assert resp.status_code == 403

    db.refresh(contact)
    assert contact.relation == "сосед"


def test_edit_contact_form_appears_on_garage_detail_page(db, client):
    owner = make_person(db, full_name="Владелец Четвёртый")
    contact_person = make_person(db, full_name="Контакт Четвёртович")
    garage = make_garage(db, number="205")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact_person.id, relation="сосед"))
    make_user(db, "board402", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board402", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    body = resp.get_data(as_text=True)
    assert "Изменить контактное лицо" in body
    assert "/edit\"" in body
