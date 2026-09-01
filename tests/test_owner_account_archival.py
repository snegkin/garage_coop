"""
Смена собственника гаража, оставшегося вовсе без собственников (единственный
выбыл — например, умер, пока наследники не оформились): его лицевые счета
не удаляются (см. garages._remove_owner_and_redistribute), но как только
появляется новый собственник, они должны заархивироваться, а новому —
завестись счета с ТЕМИ ЖЕ номерами, с переносом остатка (см.
garages._archive_owner_accounts_and_reuse, accounting.transfer_member_account_balance).
"""
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, GarageOwnership, Charge, Payment
from app import database
from app.accounting import balance

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_fee_type(db):
    fee_type = FeeType(code="membership", name="Членский взнос", type_code="1")
    db.add(fee_type)
    db.flush()
    return fee_type


def test_new_owner_reuses_number_and_inherits_debt(app, db, client):
    old_owner = make_person(db, full_name="Умерший Иван Иванович")
    new_owner = make_person(db, full_name="Наследник Пётр Петрович")
    garage = make_garage(db, number="200")
    ownership = make_ownership(db, garage, old_owner)
    fee_type = _setup_fee_type(db)

    old_account = MemberAccount(
        person_id=old_owner.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="12000",
    )
    db.add(old_account)
    db.flush()
    db.add(Charge(account_id=old_account.id, year=2026, amount=Decimal("1500.00")))  # долг, платежей не было
    make_user(db, "board_archive", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_archive", "pass12345")

    assert balance(old_account) == Decimal("-1500.00")

    # выбытие единственного собственника — счета не трогаются
    resp = client.post(
        f"/garages/{garage.id}/owners/{ownership.id}/remove", data={"comment": "умер"},
    )
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).count() == 0
    refreshed_old = database.db_session.get(MemberAccount, old_account.id)
    assert refreshed_old.is_archived is False
    assert balance(refreshed_old) == Decimal("-1500.00")

    # новый собственник — счёт должен заархивироваться и переиспользоваться
    resp = client.post(
        f"/garages/{garage.id}/owners/add", data={"person_id": new_owner.id, "share": "1"},
    )
    assert resp.status_code == 302
    db.expire_all()

    accounts = (
        database.db_session.query(MemberAccount)
        .filter_by(garage_id=garage.id, fee_type_id=fee_type.id)
        .all()
    )
    assert len(accounts) == 2
    old_acc = next(a for a in accounts if a.person_id == old_owner.id)
    new_acc = next(a for a in accounts if a.person_id == new_owner.id)

    assert old_acc.is_archived is True
    assert new_acc.is_archived is False
    assert old_acc.account_number == "12000"
    assert new_acc.account_number == "12000"          # номер переиспользован
    assert balance(old_acc) == Decimal("0.00")         # долг снят с архивного
    assert balance(new_acc) == Decimal("-1500.00")     # и перенесён на новый

    # новый собственник теперь единственный владелец записи GarageOwnership
    ownerships = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).all()
    assert len(ownerships) == 1
    assert ownerships[0].person_id == new_owner.id


def test_new_owner_gets_fresh_number_when_garage_never_lost_all_owners(app, db, client):
    """Обычное добавление СОВЛАДЕЛЬЦА (у гаража уже есть собственник) —
    поведение не должно меняться: новый номер, не архивация чужого счёта."""
    owner_a = make_person(db, full_name="Совладелец Один")
    owner_b = make_person(db, full_name="Совладелец Два")
    garage = make_garage(db, number="201")
    make_ownership(db, garage, owner_a, share="1")
    fee_type = _setup_fee_type(db)
    account_a = MemberAccount(
        person_id=owner_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="12010",
    )
    db.add(account_a)
    make_user(db, "board_coowner", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_coowner", "pass12345")

    resp = client.post(
        f"/garages/{garage.id}/owners/add", data={"person_id": owner_b.id, "share": "0.5"},
    )
    assert resp.status_code == 302
    db.expire_all()

    account_b = (
        database.db_session.query(MemberAccount)
        .filter_by(garage_id=garage.id, person_id=owner_b.id, fee_type_id=fee_type.id)
        .first()
    )
    assert account_b is not None
    assert account_b.account_number != "12010"
    assert account_b.is_archived is False
    refreshed_a = database.db_session.get(MemberAccount, account_a.id)
    assert refreshed_a.is_archived is False
    assert refreshed_a.account_number == "12010"


def test_removing_and_readding_the_same_sole_owner_does_not_duplicate_accounts(app, db, client):
    """Регрессия: собственника по ошибке удалили и тут же вернули обратно
    (тот же человек, гараж побывал без собственника мгновение) — его счета
    всё это время оставались активными (см. _remove_owner_and_redistribute),
    архивировать/дублировать нечего. Раньше это падало с UNIQUE constraint
    failed по (person_id, garage_id, fee_type_id) — код пытался завести
    ему «новый» счёт с тем же номером поверх его же активного."""
    person = make_person(db, full_name="Тот Же Собственник")
    garage = make_garage(db, number="202")
    ownership = make_ownership(db, garage, person)
    fee_type = _setup_fee_type(db)
    account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="12020",
    )
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("300.00")))
    make_user(db, "board_readd", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_readd", "pass12345")

    resp = client.post(f"/garages/{garage.id}/owners/{ownership.id}/remove", data={"comment": "ошибка"})
    assert resp.status_code == 302

    resp = client.post(f"/garages/{garage.id}/owners/add", data={"person_id": person.id, "share": "1"})
    assert resp.status_code == 302
    db.expire_all()

    accounts = (
        database.db_session.query(MemberAccount)
        .filter_by(garage_id=garage.id, fee_type_id=fee_type.id)
        .all()
    )
    assert len(accounts) == 1                      # не задублировался
    assert accounts[0].id == account.id
    assert accounts[0].is_archived is False
    assert accounts[0].account_number == "12020"
    assert balance(accounts[0]) == Decimal("-300.00")  # долг никуда не делся, не обнулился
