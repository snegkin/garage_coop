"""Лицевые счета на карточке человека: у собственника — и общий счёт его
гаража за электричество (PersonalAccount) с балансом гаража, чтобы долг/
переплату по электричеству было видно без перехода на гараж. У лица для
связи этого гаража строки нет."""
from decimal import Decimal

from app.models import RoleEnum, Charge, PersonalAccount, GarageContact

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_owner_sees_garage_electricity_account(app, db, client):
    first = make_person(db, full_name="Первый Совладелец")
    second = make_person(db, full_name="Второй Совладелец")
    contact = make_person(db, full_name="Лицо Связи")
    garage = make_garage(db, number="42")
    make_ownership(db, garage, first, share="0.5")
    make_ownership(db, garage, second, share="0.5")
    db.add(GarageContact(garage_id=garage.id, person_id=contact.id))
    db.add(PersonalAccount(garage_id=garage.id, account_number="90042"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("1234.50")))
    make_user(db, "board_el", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_el", "pass12345")

    for person in (first, second):
        html = client.get(f"/persons/{person.id}").get_data(as_text=True)
        assert f'data-electricity-row="{garage.id}"' in html
        row = html.split(f'data-electricity-row="{garage.id}"')[1].split("</tr>")[0]
        assert "90042" in row
        assert "-1234,50 ₽" in row  # баланс гаража, долг целиком (не доля)

    html = client.get(f"/persons/{contact.id}").get_data(as_text=True)
    assert "data-electricity-row" not in html
