"""
Тесты на журнал аудита: денежные/ролевые операции создают запись с верным
actor'ом, журнал виден правлению и недоступен рядовому члену.

Также покрыты недавние правки удобства чтения журнала (app/audit.py:
format_amount/format_date/entity_url, governance/audit_log.html): ФИО в
текстах записей сокращены до "Фамилия И.О." (person.short_name — та же
логика, что уже применялась в комментариях зачёта между счетами, см.
finance.transfer_member_account_funds), суммы и даты — в фиксированном
русском формате независимо от локали действующего лица/смотрящего, и
ссылка "↗" на карточку сущности (человек/контрагент/счёт) там, где она есть.
"""
import datetime as dt
import re
from decimal import Decimal

from app import audit
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
    assert "500,00 ₽" in entries[0].summary  # audit.format_amount — всегда с запятой и ₽, вне зависимости от локали


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


def test_format_amount_is_locale_independent_comma_with_currency():
    assert audit.format_amount(Decimal("1234.5")) == "1234,50 ₽"
    assert audit.format_amount(1000) == "1000,00 ₽"


def test_format_date_is_always_russian_dd_mm_yyyy():
    assert audit.format_date(dt.date(2026, 3, 5)) == "05.03.2026"


def test_entity_url_known_types(app):
    with app.test_request_context():
        assert audit.entity_url("person", 7).endswith("/persons/7")
        assert audit.entity_url("counterparty", 3).endswith("/counterparties/3")
        assert audit.entity_url("member_account", 9).endswith("/finance/member-accounts/9")


def test_entity_url_unknown_type_or_missing_id_returns_none(app):
    with app.test_request_context():
        assert audit.entity_url("bank_account", 1) is None
        assert audit.entity_url("person", None) is None
        assert audit.entity_url(None, None) is None


def test_charge_summary_uses_short_name_not_full_name(app, db, client):
    person = make_person(db, full_name="Долгополов Иван Петрович")
    garage = make_garage(db, number="80")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="short1", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="20001")
    db.add(account)
    board_person = make_person(db, full_name="Board Two")
    make_user(db, "boarduser2", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser2", "pass1234")
    client.post(f"/finance/member-accounts/{account.id}/charges/add", data={"year": "2026", "amount": "100.00"})

    entry = db.query(AuditLog).filter_by(action="charge.create").one()
    assert "Долгополов И.П." in entry.summary
    assert "Долгополов Иван Петрович" not in entry.summary


def test_audit_log_page_shows_entity_link_for_person(app, db, client):
    person = make_person(db, full_name="Ссылков Пётр Ссылкович")
    make_user(db, "chair2", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "chair2", "pass1234")
    reason = "тестовая причина"
    client.post(f"/persons/{person.id}/archive", data={"reason": reason})

    resp = client.get("/governance/audit-log")
    html = resp.get_data(as_text=True)
    assert f'href="/persons/{person.id}"' in html
    assert "Ссылков П.С." in html
