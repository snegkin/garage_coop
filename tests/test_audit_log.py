"""
Тесты на журнал аудита: денежные/ролевые операции создают запись с верным
actor'ом, журнал виден правлению и недоступен рядовому члену.
"""
import re
from decimal import Decimal

from app.models import RoleEnum, AuditLog, FeeType, MemberAccount

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else ""


def test_member_charge_creates_audit_entry(app, db, client):
    person = make_person(db)
    garage = make_garage(db)
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10001")
    db.add(account)
    board_person = make_person(db, full_name="Board")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/add",
        data={"year": "2026", "amount": "500.00"},
    )
    assert resp.status_code == 302

    entries = db.query(AuditLog).filter_by(action="charge.create").all()
    assert len(entries) == 1
    assert entries[0].actor_username == "boarduser"
    assert "500.00" in entries[0].summary


def test_login_creates_audit_entry_with_correct_actor(app, db, client):
    board_person = make_person(db, full_name="Board")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")

    entries = db.query(AuditLog).filter_by(action="auth.login").all()
    assert len(entries) == 1
    assert entries[0].actor_username == "boarduser"


def test_failed_login_creates_audit_entry(app, db, client):
    board_person = make_person(db, full_name="Board")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "wrong-password")

    entries = db.query(AuditLog).filter_by(action="auth.login_failed").all()
    assert len(entries) == 1
    assert "boarduser" in entries[0].summary
    assert entries[0].actor_user_id is None  # неудачный логин — актёр неизвестен


def test_role_change_creates_audit_entry(app, db, client):
    person = make_person(db, full_name="Future Board Member")
    user = make_user(db, "futureboard", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()

    from app.permissions import sync_user_role
    person.is_board_member = True
    sync_user_role(person)
    db.commit()

    entries = db.query(AuditLog).filter_by(action="role.change").all()
    assert len(entries) == 1
    assert "member" in entries[0].summary
    assert "board" in entries[0].summary


def test_audit_log_visible_to_board(app, db, client):
    board_person = make_person(db, full_name="Board")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp = client.get("/governance/audit-log")
    assert resp.status_code == 200


def test_audit_log_hidden_from_member(app, db, client):
    person = make_person(db)
    garage = make_garage(db)
    make_ownership(db, garage, person)
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()

    login(client, "member1", "pass1234")
    resp = client.get("/governance/audit-log")
    assert resp.status_code == 302


def test_password_reset_creates_audit_entry(app, db, client):
    person = make_person(db, full_name="Some Member")
    member_user = make_user(db, "targetuser", "oldpass123", role=RoleEnum.MEMBER, person=person)
    chairman_person = make_person(db, full_name="Chairman")
    make_user(db, "chairuser", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman_person)
    db.commit()

    login(client, "chairuser", "pass1234")
    resp = client.post(f"/persons/{person.id}/account/reset-password", data={"password": "newpass456"})
    assert resp.status_code == 302

    entries = db.query(AuditLog).filter_by(action="account.password_reset").all()
    assert len(entries) == 1
    assert entries[0].actor_username == "chairuser"
    assert "targetuser" in entries[0].summary
