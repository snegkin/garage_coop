"""
Сводка над реестрами гаражей и людей (garages.list_garages,
persons._person_stats) и флаг «арендатор» у лица для связи
(GarageContact.is_tenant).
"""
import datetime as dt
import re

from app.models import ElectricityMeter, GarageContact, RoleEnum

from tests.conftest import login, make_garage, make_ownership, make_person, make_user


def _board(db, client):
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=make_person(db, full_name="Член Правления"))
    db.commit()
    login(client, "board1", "pass1234")


def _stats(html: str) -> dict[str, int]:
    """{подпись: число} из плашек сводки."""
    pairs = re.findall(r'<div class="fs-5 fw-semibold lh-1">(\d+)</div>\s*<div class="small text-muted">([^<]+)</div>', html)
    return {label: int(value) for value, label in pairs}


def test_garage_stats(db, client):
    _board(db, client)
    g1 = make_garage(db, "1", land_privatized=True)
    make_garage(db, "2")
    make_garage(db, "3", land_privatized=True)
    db.add(ElectricityMeter(garage_id=g1.id, meter_number="A-1", installed_date=dt.date(2025, 1, 1)))
    db.commit()

    stats = _stats(client.get("/garages/").get_data(as_text=True))
    assert stats == {"всего гаражей": 3, "с электричеством": 1, "без электричества": 2,
                     "участок приватизирован": 2, "участок не приватизирован": 1,
                     "на территории": 3, "вне территории": 0}


def test_person_stats(db, client):
    _board(db, client)  # член правления — сам по себе ни собственник, ни контакт
    g1, g2 = make_garage(db, "1"), make_garage(db, "2")
    owner = make_person(db, full_name="Собственник")
    make_ownership(db, g1, owner)
    make_ownership(db, g2, owner)
    # Собственник одного гаража и лицо для связи по другому — в обеих группах.
    co = make_person(db, full_name="Совладелец")
    make_ownership(db, g2, co, share="0.5")
    db.add(GarageContact(garage_id=g1.id, person_id=co.id, relation="брат"))
    tenant = make_person(db, full_name="Арендатор")
    db.add(GarageContact(garage_id=g2.id, person_id=tenant.id, is_tenant=True))
    archived = make_person(db, full_name="Архивный", is_archived=True)
    db.add(GarageContact(garage_id=g1.id, person_id=archived.id, is_tenant=True))
    db.commit()

    stats = _stats(client.get("/persons/").get_data(as_text=True))
    assert stats == {"всего людей": 4, "собственники": 2, "для связи": 1, "арендаторы": 1}


def test_contact_tenant_flag_add_and_edit(db, client):
    _board(db, client)
    garage = make_garage(db, "7")
    person = make_person(db, full_name="Арендатор")
    db.commit()

    client.post(f"/garages/{garage.id}/contacts/add",
                data={"person_id": person.id, "is_tenant": "1", "relation": "Арендатор"})
    contact = db.query(GarageContact).one()
    assert contact.is_tenant is True
    # Отношение — «арендатор», без повтора введённого и без прочерка.
    assert "<td>арендатор</td>" in client.get(f"/garages/{garage.id}").get_data(as_text=True)
    contact.relation = "до 2027 года"
    db.commit()
    assert "<td>арендатор · до 2027 года</td>" in client.get(f"/garages/{garage.id}").get_data(as_text=True)

    client.post(f"/garages/{garage.id}/contacts/{contact.id}/edit", data={"person_id": person.id, "relation": "друг"})
    db.refresh(contact)
    assert contact.is_tenant is False and contact.relation == "друг"


def test_stat_tiles_filter_rows(db, client):
    """Плашки — ссылки ?group=…; строки размечены группами для фильтра на месте."""
    _board(db, client)
    g1 = make_garage(db, "1", land_privatized=True)
    make_garage(db, "2")
    db.add(ElectricityMeter(garage_id=g1.id, meter_number="A-1", installed_date=dt.date(2025, 1, 1)))
    tenant = make_person(db, full_name="Арендатор")
    db.add(GarageContact(garage_id=g1.id, person_id=tenant.id, is_tenant=True))
    db.commit()

    html = client.get("/garages/?group=power").get_data(as_text=True)
    assert 'data-groups="inside power privatized"' in html and 'data-groups="inside nopower notprivatized"' in html
    assert re.search(r'class="list-stat[^"]*is-active"\s+href="\?group=power"', html)

    html = client.get("/persons/").get_data(as_text=True)
    assert 'data-groups="tenant"' in html and 'href="?group=tenant"' in html
    # В архиве плашки ведут на «Все» с фильтром.
    html = client.get("/persons/?archived=1").get_data(as_text=True)
    assert 'href="/persons/?group=tenant"' in html
