"""
Списание пени (finance.write_off_penalty) — для случаев, когда кооператив
отказывается от взыскания (мировое соглашение, добровольный отказ от
претензий). Регистрируется как погашающий платёж на всю непогашенную сумму
пени (реальных денег не движется, счёт обнуляется — тот же приём, что и у
компенсирующих проводок в accounting.py: transfer_member_account_balance/
redistribute_member_account_balance). Доступно только председателю и
бухгалтеру (is_privileged()) — рядовому члену правления (роль BOARD) нельзя,
хотя формально она на одном уровне с ACCOUNTANT по auth.ROLE_LEVEL.
"""
import datetime as dt
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, AuditLog

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_penalty_account(db, balance=Decimal("-472.53")):
    """Пеня-счёт с начислением на -balance (без платежей) — баланс ровно balance."""
    person = make_person(db, full_name="Должников Должник Должникович")
    garage = make_garage(db, number="70")
    make_ownership(db, garage, person)
    penalty = FeeType(code="membership_penalty", name="Пеня по взносу", type_code="1", is_penalty=True)
    db.add(penalty)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П17001")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=-balance))
    db.flush()
    return account


def test_chairman_can_write_off_penalty(app, db, client):
    account = _setup_penalty_account(db)
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/write-off-penalty", data={
        "reason": "Мировое соглашение от 01.09.2026",
    })
    assert resp.status_code == 302

    db.expire_all()
    payments = db.query(Payment).filter_by(account_id=account.id).all()
    assert len(payments) == 1
    assert payments[0].amount == Decimal("472.53")
    assert "Мировое соглашение" in payments[0].comment

    entries = db.query(AuditLog).filter_by(action="penalty.write_off").all()
    assert len(entries) == 1
    assert entries[0].actor_username == "chair1"


def test_accountant_can_write_off_penalty(app, db, client):
    account = _setup_penalty_account(db)
    make_user(db, "acc1", "pass12345", role=RoleEnum.ACCOUNTANT)
    db.commit()
    login(client, "acc1", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/write-off-penalty", data={
        "reason": "Отказ от взыскания",
    })
    assert resp.status_code == 302
    assert db.query(Payment).filter_by(account_id=account.id).count() == 1


def test_plain_board_member_cannot_write_off_penalty(app, db, client):
    """BOARD и ACCOUNTANT на одном уровне по ROLE_LEVEL, но списание — только
    is_privileged() (председатель/бухгалтер), рядовому правлению нельзя."""
    account = _setup_penalty_account(db)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get(f"/finance/member-accounts/{account.id}")
    assert resp.status_code == 200
    assert "writeOffPenaltyModal" not in resp.get_data(as_text=True)

    resp = client.post(f"/finance/member-accounts/{account.id}/write-off-penalty", data={
        "reason": "Пытаюсь списать",
    })
    assert resp.status_code == 403
    assert db.query(Payment).filter_by(account_id=account.id).count() == 0


def test_write_off_rejected_without_reason(app, db, client):
    account = _setup_penalty_account(db)
    make_user(db, "chair2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair2", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/write-off-penalty", data={"reason": ""})
    assert resp.status_code == 302
    assert db.query(Payment).filter_by(account_id=account.id).count() == 0


def test_write_off_rejected_for_non_penalty_account(app, db, client):
    person = make_person(db, full_name="Обычный Взносник")
    garage = make_garage(db, number="71")
    make_ownership(db, garage, person)
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    db.add(membership)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="17100")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("500.00")))
    make_user(db, "chair3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair3", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/write-off-penalty", data={
        "reason": "Пробую списать обычный взнос",
    })
    assert resp.status_code == 302
    assert db.query(Payment).filter_by(account_id=account.id).count() == 0


def test_write_off_rejected_when_nothing_owed(app, db, client):
    """Пеня уже погашена (баланс 0) — списывать нечего, кнопки/действия нет."""
    person = make_person(db, full_name="Без Долга По Пене")
    garage = make_garage(db, number="72")
    make_ownership(db, garage, person)
    penalty = FeeType(code="membership_penalty2", name="Пеня по взносу", type_code="1", is_penalty=True)
    db.add(penalty)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П17200")
    db.add(account)
    db.flush()
    make_user(db, "chair4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair4", "pass12345")

    resp = client.get(f"/finance/member-accounts/{account.id}")
    assert resp.status_code == 200
    assert "writeOffPenaltyModal" not in resp.get_data(as_text=True)

    resp = client.post(f"/finance/member-accounts/{account.id}/write-off-penalty", data={
        "reason": "Пытаюсь списать нулевой баланс",
    })
    assert resp.status_code == 302
    assert db.query(Payment).filter_by(account_id=account.id).count() == 0
