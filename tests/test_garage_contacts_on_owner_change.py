"""
Лица для связи гаража — контакты прежних собственников: когда выбывают ВСЕ
собственники (последний удалён с карточки, человек отправлен в архив,
гараж целиком передан новому), они удаляются с карточки
(garages._drop_contacts_of_ownerless). Пока остаётся хоть один из прежних
совладельцев — контакты сохраняются.
"""
from app import database
from app.garages import transfer_owner
from app.models import RoleEnum, GarageContact, Person

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _contacts(garage_id):
    database.db_session.expire_all()
    return database.db_session.query(GarageContact).filter_by(garage_id=garage_id).count()


def _setup(db, *owners):
    garage = make_garage(db, number="156")
    ownerships = [make_ownership(db, garage, make_person(db, full_name=n), share=str(1 / len(owners)))
                  for n in owners]
    relative = make_person(db, full_name="Лидия Викторовна")
    db.add(GarageContact(garage_id=garage.id, person_id=relative.id, relation="жена"))
    make_user(db, "board_contacts", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    return garage, ownerships, relative


def test_last_owner_removed_drops_contacts(app, db, client):
    garage, (o1, o2), relative = _setup(db, "Земсков А. В.", "Земсков Б. В.")
    login(client, "board_contacts", "pass12345")

    client.post(f"/garages/{garage.id}/owners/{o1.id}/remove", data={"comment": "продал долю"})
    assert _contacts(garage.id) == 1          # совладелец остался — контакт нужен

    client.post(f"/garages/{garage.id}/owners/{o2.id}/remove", data={"comment": "продал"})
    assert _contacts(garage.id) == 0
    assert database.db_session.get(Person, relative.id) is not None   # сам человек остаётся


def test_archiving_sole_owner_drops_contacts(app, db, client):
    garage, (owner,), _ = _setup(db, "Земсков А. В.")
    login(client, "board_contacts", "pass12345")
    client.post(f"/persons/{owner.person_id}/archive", data={"reason": "продал гараж"})
    assert _contacts(garage.id) == 0


def test_transfer_from_sole_owner_drops_contacts(app, db):
    garage, (owner,), _ = _setup(db, "Земсков А. В.")
    new = make_person(db, full_name="Монахов Ю. А.")
    with app.test_request_context():
        from flask import g
        from app.models import User
        g.user = database.db_session.query(User).first()
        transfer_owner(garage, owner, new.id, carry_balance=False, comment="продажа", user_id=g.user.id)
        database.db_session.commit()
    assert _contacts(garage.id) == 0


def test_transfer_of_one_share_keeps_contacts(app, db):
    garage, (o1, _o2), _ = _setup(db, "Салтыков А.", "Салтыков Б.")
    new = make_person(db, full_name="Покупатель В.")
    with app.test_request_context():
        from flask import g
        from app.models import User
        g.user = database.db_session.query(User).first()
        transfer_owner(garage, o1, new.id, carry_balance=False, comment="продажа доли", user_id=g.user.id)
        database.db_session.commit()
    assert _contacts(garage.id) == 1
