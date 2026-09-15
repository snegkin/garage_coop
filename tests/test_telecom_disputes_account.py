"""
Вид взноса "telecom_disputes" (FeeType.per_garage=False) — личный счёт
человека, не привязанный к гаражу (см. accounting.ensure_personal_member_accounts):
- заводится автоматически при появлении человека собственником гаража
  (garages.add_owner), один на человека сразу за все его гаражи;
- страницы со списком лицевых счетов (finance.member_accounts,
  persons.detail, cabinet.garages) не падают на account.garage is None;
- legal_docs.state_duty_charge вручную начисляет госпошлину на этот счёт.
"""
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _telecom_fee_type(db):
    ft = db.query(FeeType).filter_by(code="telecom_disputes").first()
    assert ft is not None, "миграция 4aafd6b5140a не применена"
    return ft


def test_add_owner_creates_personal_telecom_account(app, db, client):
    person = make_person(db, full_name="Связной Иван Иванович")
    garage = make_garage(db, number="201")
    make_user(db, "board_t1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_t1", "pass12345")

    fee_type = _telecom_fee_type(db)
    resp = client.post(f"/garages/{garage.id}/owners/add", data={"person_id": person.id, "share": "1"})
    assert resp.status_code == 302

    accounts = db.query(MemberAccount).filter_by(person_id=person.id, fee_type_id=fee_type.id).all()
    assert len(accounts) == 1
    assert accounts[0].garage_id is None


def test_add_owner_does_not_duplicate_existing_personal_account(app, db, client):
    """Второй гараж тому же человеку — счёт telecom_disputes не дублируется."""
    person = make_person(db, full_name="Связной Пётр Петрович")
    garage1 = make_garage(db, number="202")
    garage2 = make_garage(db, number="203")
    make_user(db, "board_t2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_t2", "pass12345")

    fee_type = _telecom_fee_type(db)
    client.post(f"/garages/{garage1.id}/owners/add", data={"person_id": person.id, "share": "1"})
    client.post(f"/garages/{garage2.id}/owners/add", data={"person_id": person.id, "share": "1"})

    accounts = db.query(MemberAccount).filter_by(person_id=person.id, fee_type_id=fee_type.id).all()
    assert len(accounts) == 1


def test_member_accounts_list_renders_personal_account(app, db, client):
    """finance/member_accounts.html не падает на account.garage is None."""
    person = make_person(db, full_name="Связной Сидор Сидорович")
    garage = make_garage(db, number="204")
    make_ownership(db, garage, person)
    make_user(db, "board_t3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_t3", "pass12345")

    fee_type = _telecom_fee_type(db)
    from app import database
    from app.accounting import ensure_personal_member_accounts
    ensure_personal_member_accounts(person.id)
    database.db_session.commit()

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    assert "Телекоммуникационные услуги и споры".encode() in resp.data


def test_persons_detail_renders_personal_account(app, db, client):
    person = make_person(db, full_name="Связной Фёдор Фёдорович")
    garage = make_garage(db, number="205")
    make_ownership(db, garage, person)
    make_user(db, "board_t4", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_t4", "pass12345")

    from app import database
    from app.accounting import ensure_personal_member_accounts
    ensure_personal_member_accounts(person.id)
    database.db_session.commit()

    resp = client.get(f"/persons/{person.id}")
    assert resp.status_code == 200


def test_cabinet_garages_renders_personal_account(app, db, client):
    person = make_person(db, full_name="Связной Кабинетов")
    garage = make_garage(db, number="206")
    make_ownership(db, garage, person)
    user = make_user(db, "member_t5", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()

    from app import database
    from app.accounting import ensure_personal_member_accounts
    ensure_personal_member_accounts(person.id)
    database.db_session.commit()

    login(client, "member_t5", "pass12345")
    resp = client.get("/cabinet/garages")
    assert resp.status_code == 200
    assert "Личные счета".encode() in resp.data


def test_state_duty_charge_creates_charge_on_personal_account(app, db, client):
    person = make_person(db, full_name="Должников Должник Должникович")
    garage = make_garage(db, number="207")
    make_ownership(db, garage, person)
    make_user(db, "board_t6", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_t6", "pass12345")

    from app import database
    from app.accounting import ensure_personal_member_accounts, balance
    ensure_personal_member_accounts(person.id)
    database.db_session.commit()

    resp = client.post("/legal-docs/state-duty/charge", data={
        "person_id": str(person.id),
        f"duty_amount_{person.id}": "4000",
    })
    assert resp.status_code == 302

    fee_type = _telecom_fee_type(db)
    account = db.query(MemberAccount).filter_by(person_id=person.id, fee_type_id=fee_type.id).one()
    db.expire_all()
    assert balance(account) == Decimal("-4000.00")
