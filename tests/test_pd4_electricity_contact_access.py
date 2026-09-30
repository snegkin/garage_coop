"""
Оплата электричества из личного кабинета (кнопка 💳 в /cabinet/garages
→ /pd4/print?garage_id=…) доступна не только собственнику, но и лицу для
связи по гаражу (GarageContact) — кабинет показывает ему тот же гараж и ту
же кнопку (см. cabinet.garages), а без этого переход давал 403. Правление
по-прежнему не может печатать электрические платёжки чужих гаражей.
"""
from decimal import Decimal

from app.models import Cooperative, RoleEnum, PersonalAccount, Charge, GarageContact

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup(db):
    db.add(Cooperative(full_name="Тестовый кооператив", inn="1234567890", kpp="123456789", ogrn="1234567890123"))
    owner = make_person(db, full_name="Собственник Гаражов")
    contact = make_person(db, full_name="Контакт Гаражова")
    stranger = make_person(db, full_name="Посторонний Членов")
    garage = make_garage(db, number="501")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact.id, relation="супруга"))
    db.add(PersonalAccount(garage_id=garage.id, account_number="50101"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("500.00")))
    make_user(db, "contact1", "pass12345", role=RoleEnum.MEMBER, person=contact)
    make_user(db, "stranger1", "pass12345", role=RoleEnum.BOARD, person=stranger)
    db.commit()
    return garage


def test_contact_person_can_open_electricity_slip(db, client):
    garage = _setup(db)
    login(client, "contact1", "pass12345")
    assert client.get(f"/pd4/print?garage_id={garage.id}").status_code == 200


def test_contact_person_can_download_electricity_pdf(db, client):
    garage = _setup(db)
    login(client, "contact1", "pass12345")
    resp = client.post("/pd4/print/pdf", data={"garage_id": str(garage.id)})
    assert resp.status_code != 403


def test_board_member_without_link_still_forbidden(db, client):
    garage = _setup(db)
    login(client, "stranger1", "pass12345")
    assert client.get(f"/pd4/print?garage_id={garage.id}").status_code == 403
