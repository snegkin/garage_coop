"""
Кнопка «Пеня» (Списать/Удалить) на общем списке лицевых счетов
(/finance/member-accounts) — та же механика, что и на карточке персоны
(см. test_person_penalty_bulk_actions.py), но без фильтра по человеку: сразу
по ВСЕМ пенным счетам кооператива. См.
finance.write_off_all_penalties/delete_all_penalties.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, AuditLog

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_penalty_account(db, person, garage, code, amount):
    fee_type = FeeType(code=code, name="Пеня по взносу", type_code=code, is_penalty=True)
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=f"П{code}")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal(amount)))
    return account


def _setup_two_persons_with_penalties(db):
    """Два РАЗНЫХ человека, каждый со своим пенным счётом — чтобы отличить
    «по всем счетам кооператива» от «по одному человеку» (последнее уже
    покрыто test_person_penalty_bulk_actions.py)."""
    person_a = make_person(db, full_name="Штрафнов Андрей Андреевич")
    garage_a = make_garage(db, number="90")
    make_ownership(db, garage_a, person_a)
    account_a = _make_penalty_account(db, person_a, garage_a, "acct_pen_a", "100.00")

    person_b = make_person(db, full_name="Пенькова Вера Викторовна")
    garage_b = make_garage(db, number="91")
    make_ownership(db, garage_b, person_b)
    account_b = _make_penalty_account(db, person_b, garage_b, "acct_pen_b", "60.00")

    db.flush()
    return [account_a, account_b]


def test_write_off_all_penalties_across_persons(app, db, client):
    accounts = _setup_two_persons_with_penalties(db)
    make_user(db, "chair90", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair90", "pass12345")

    resp = client.post("/finance/member-accounts/write-off-penalties", data={"reason": "Общее мировое соглашение"})
    assert resp.status_code == 302
    db.expire_all()

    payments = db.query(Payment).filter(Payment.account_id.in_([a.id for a in accounts])).all()
    assert len(payments) == 2
    assert {p.amount for p in payments} == {Decimal("100.00"), Decimal("60.00")}


def test_write_off_all_penalties_requires_privileged(app, db, client):
    accounts = _setup_two_persons_with_penalties(db)
    make_user(db, "board90", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board90", "pass12345")

    resp = client.post("/finance/member-accounts/write-off-penalties", data={"reason": "Пытаюсь списать"})
    assert resp.status_code == 403
    db.expire_all()
    assert db.query(Payment).filter(Payment.account_id.in_([a.id for a in accounts])).count() == 0


def test_write_off_all_penalties_rejected_without_reason(app, db, client):
    accounts = _setup_two_persons_with_penalties(db)
    make_user(db, "chair91", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair91", "pass12345")

    resp = client.post("/finance/member-accounts/write-off-penalties", data={"reason": ""})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(Payment).filter(Payment.account_id.in_([a.id for a in accounts])).count() == 0


def test_delete_all_penalties_across_persons(app, db, client):
    accounts = _setup_two_persons_with_penalties(db)
    make_user(db, "chair92", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair92", "pass12345")

    resp = client.post("/finance/member-accounts/delete-penalties")
    assert resp.status_code == 302
    db.expire_all()

    remaining = db.query(Charge).filter(Charge.account_id.in_([a.id for a in accounts])).count()
    assert remaining == 0
    entries = db.query(AuditLog).filter_by(action="penalty.delete_all").all()
    assert len(entries) == 1
    assert entries[0].entity_type == "cooperative"


def test_delete_all_penalties_requires_chairman(app, db, client):
    accounts = _setup_two_persons_with_penalties(db)
    make_user(db, "acc90", "pass12345", role=RoleEnum.ACCOUNTANT)
    db.commit()
    login(client, "acc90", "pass12345")

    resp = client.post("/finance/member-accounts/delete-penalties")
    assert resp.status_code == 302
    db.expire_all()
    remaining = db.query(Charge).filter(Charge.account_id.in_([a.id for a in accounts])).count()
    assert remaining == 2  # не удалено


def test_member_accounts_page_shows_penalty_dropdown_only_when_unpaid(app, db, client):
    accounts = _setup_two_persons_with_penalties(db)
    make_user(db, "chair93", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair93", "pass12345")

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    assert "writeOffAllPenaltiesModal" in resp.get_data(as_text=True)

    for acc in accounts:
        db.add(Payment(account_id=acc.id, date=dt.date(2026, 1, 1), amount=acc.charges[0].amount))
    db.commit()
    from app.accounting import reallocate_member_charges
    for acc in accounts:
        reallocate_member_charges(acc)
    db.commit()

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    assert "writeOffAllPenaltiesModal" not in resp.get_data(as_text=True)
