"""
Правка уже проведённого начисления (finance.edit_member_charge) — для
случая, когда начисление было посчитано по неверной формуле (например,
массовое начисление земельного налога по приватизированным гаражам за
конкретный год) и его нужно исправить точечно, не откатывая всё
начисление целиком. Доступ — is_board(), как у «Начислить».
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import balance
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_account(db, account_number="19001", garage_number="90", fee_type_code="land_tax"):
    person = make_person(db, full_name="Земельникова Земля Земельевна")
    garage = make_garage(db, number=garage_number)
    make_ownership(db, garage, person)
    fee_type = FeeType(code=fee_type_code, name="Земельный налог")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=account_number)
    db.add(account)
    db.flush()
    return account


def test_edit_member_charge_updates_amount_and_reallocates(app, db, client):
    account = _setup_account(db)
    charge = Charge(account_id=account.id, year=2026, amount=Decimal("1000.00"), comment="Старый расчёт")
    db.add(charge)
    db.flush()
    db.add(Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("1000.00")))
    make_user(db, "board10", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board10", "pass12345")

    assert balance(account) == Decimal("0.00")  # начислено 1000, оплачено 1000

    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/{charge.id}/edit",
        data={"year": "2026", "amount": "700.00", "comment": "Исправлено — приватизированный гараж"},
    )
    assert resp.status_code == 302
    db.expire_all()

    updated = database.db_session.get(Charge, charge.id)
    assert updated.amount == Decimal("700.00")
    assert updated.comment == "Исправлено — приватизированный гараж"
    # переплата 300 после уменьшения начисления — reallocate_member_charges пересчитан
    assert balance(account) == Decimal("300.00")


def test_edit_member_charge_rejects_invalid_amount(app, db, client):
    account = _setup_account(db)
    charge = Charge(account_id=account.id, year=2026, amount=Decimal("500.00"))
    db.add(charge)
    make_user(db, "board11", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board11", "pass12345")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/{charge.id}/edit",
        data={"year": "2026", "amount": "0"},
    )
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Charge, charge.id).amount == Decimal("500.00")  # не изменилось


def test_edit_member_charge_requires_board(db, client):
    """@roles_required редиректит с flash (302), а не 403 — тот же контракт,
    что и у остальных роутов finance.py на этом декораторе (см.
    test_audit_log.py)."""
    account = _setup_account(db)
    charge = Charge(account_id=account.id, year=2026, amount=Decimal("500.00"))
    db.add(charge)
    person = database.db_session.get(MemberAccount, account.id).person
    make_user(db, "member10", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member10", "pass12345")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/{charge.id}/edit",
        data={"year": "2026", "amount": "999.00"},
    )
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Charge, charge.id).amount == Decimal("500.00")


def test_edit_member_charge_rejects_charge_from_another_account(app, db, client):
    account = _setup_account(db, account_number="19002", garage_number="91", fee_type_code="land_tax_a")
    other_account = _setup_account(db, account_number="19003", garage_number="92", fee_type_code="land_tax_b")
    charge = Charge(account_id=other_account.id, year=2026, amount=Decimal("500.00"))
    db.add(charge)
    make_user(db, "board12", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board12", "pass12345")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/{charge.id}/edit",
        data={"year": "2026", "amount": "999.00"},
    )
    assert resp.status_code == 404
    db.expire_all()
    assert database.db_session.get(Charge, charge.id).amount == Decimal("500.00")
