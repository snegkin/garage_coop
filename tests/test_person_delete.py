"""
Полное удаление человека (кнопка «Удалить» в редактировании) — только
председатель, только с галочкой-подтверждением; удаляется всё, что на нём:
телефоны, доли в гаражах, лица для связи, лицевые счета с начислениями и
оплатами. Учётная запись блокируется и отвязывается, сам гараж остаётся.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import (
    RoleEnum, Person, Phone, MemberAccount, FeeType, Charge, Payment, ChargeAllocation, GarageOwnership,
    GarageContact, Garage, User,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup(db):
    person = make_person(db, full_name="Удаляемый Иван")
    db.add(Phone(person_id=person.id, number="+79990000000"))
    garage = make_garage(db, "77")
    other = make_garage(db, "78")
    make_ownership(db, garage, person)
    db.add(GarageContact(garage_id=other.id, person_id=person.id, relation="сын", is_tenant=False))
    ft = FeeType(code="10", name="Членский взнос", type_code="1")
    db.add(ft)
    db.flush()
    acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=ft.id, account_number="X-1")
    db.add(acc)
    db.flush()
    charge = Charge(account_id=acc.id, year=2026, amount=Decimal("1000"))
    pay = Payment(account_id=acc.id, date=dt.date(2026, 2, 1), amount=Decimal("1000"))
    db.add_all([charge, pay])
    db.flush()
    db.add(ChargeAllocation(charge_id=charge.id, payment_id=pay.id, amount=Decimal("1000")))
    make_user(db, "ivan", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    return person, garage


def test_chairman_deletes_person_with_everything(app, db, client):
    person, garage = _setup(db)
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair1", "pass12345")

    page = client.get(f"/persons/{person.id}/edit").get_data(as_text=True)
    assert "deletePersonModal" in page and "собственник гаражей" in page

    # без галочки — ничего не удаляется
    client.post(f"/persons/{person.id}/delete", data={})
    assert database.db_session.get(Person, person.id) is not None

    resp = client.post(f"/persons/{person.id}/delete", data={"confirm": "1"})
    assert resp.status_code == 302
    s = database.db_session
    s.expire_all()
    assert s.get(Person, person.id) is None
    assert s.query(Phone).count() == 0
    assert s.query(GarageOwnership).count() == 0
    assert s.query(GarageContact).count() == 0
    assert s.query(MemberAccount).count() == 0
    assert s.query(Charge).count() == 0
    assert s.query(Payment).count() == 0
    assert s.get(Garage, garage.id) is not None
    user = s.query(User).filter_by(username="ivan").one()
    assert user.person_id is None and user.is_active is False


def test_board_member_cannot_delete(app, db, client):
    person, _ = _setup(db)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    assert "deletePersonModal" not in client.get(f"/persons/{person.id}/edit").get_data(as_text=True)
    client.post(f"/persons/{person.id}/delete", data={"confirm": "1"})
    database.db_session.expire_all()
    assert database.db_session.get(Person, person.id) is not None


def test_chairman_cannot_delete_himself(app, db, client):
    me = make_person(db, full_name="Председатель Сам")
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN, person=me)
    db.commit()
    login(client, "chair1", "pass12345")

    client.post(f"/persons/{me.id}/delete", data={"confirm": "1"})
    database.db_session.expire_all()
    assert database.db_session.get(Person, me.id) is not None
