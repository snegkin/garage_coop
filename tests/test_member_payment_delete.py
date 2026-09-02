"""
Удаление ошибочно/задвоенно внесённого платежа (finance.delete_member_payment)
— доступ только председателю (CHAIRMAN), в отличие от добавления платежа
(is_board()) и правки начисления (тоже is_board()) — удаление денежной
записи чувствительнее, чем правка её суммы.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import balance
from app.models import (
    RoleEnum, FeeType, MemberAccount, Charge, Payment,
    BankAccount, BankStatementLine, BankApiProvider,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_account(db, account_number="19501", garage_number="95", fee_type_code="membership"):
    person = make_person(db, full_name="Платежов Платон Платонович")
    garage = make_garage(db, number=garage_number)
    make_ownership(db, garage, person)
    fee_type = FeeType(code=fee_type_code, name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=account_number)
    db.add(account)
    db.flush()
    return account


def test_delete_member_payment_removes_it_and_reallocates(app, db, client):
    account = _setup_account(db)
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("1000.00")))
    payment = Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("1000.00"))
    db.add(payment)
    db.flush()
    make_user(db, "chair40", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair40", "pass12345")

    assert balance(account) == Decimal("0.00")  # начислено 1000, оплачено 1000
    db.expire_all()  # иначе account.payments в сессии остаётся с уже удалённым платежом

    resp = client.post(f"/finance/member-accounts/{account.id}/payments/{payment.id}/delete")
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(Payment, payment.id) is None
    assert balance(account) == Decimal("-1000.00")  # платёж удалён — снова долг


def test_delete_member_payment_unlinks_matched_statement_line(app, db, client):
    """Строка выписки, разнесённая на этот платёж, не удаляется — только
    теряет ссылку (ondelete=SET NULL) и возвращается в «не разнесён»."""
    account = _setup_account(db, account_number="19502", garage_number="96")
    payment = Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("500.00"))
    db.add(payment)
    db.flush()

    bank_account = BankAccount(bank_name="Тестбанк", checking_account="40703810000000000001", api_provider=BankApiProvider.NONE)
    db.add(bank_account)
    db.flush()
    line = BankStatementLine(
        bank_account_id=bank_account.id, external_uid="op-del-1", operation_date=dt.date(2026, 1, 1),
        direction="credit", amount=Decimal("500.00"), matched_payment_id=payment.id,
    )
    db.add(line)
    make_user(db, "chair41", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair41", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/payments/{payment.id}/delete")
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(Payment, payment.id) is None
    updated_line = database.db_session.get(BankStatementLine, line.id)
    assert updated_line is not None  # строка выписки осталась
    assert updated_line.matched_payment_id is None  # но ссылка обнулилась


def test_delete_member_payment_requires_chairman(db, client):
    """@roles_required(CHAIRMAN) редиректит с flash (302), а не 403."""
    account = _setup_account(db, account_number="19503", garage_number="97")
    payment = Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("300.00"))
    db.add(payment)
    db.flush()
    person = database.db_session.get(MemberAccount, account.id).person
    make_user(db, "board20", "pass12345", role=RoleEnum.BOARD, person=person)
    db.commit()
    login(client, "board20", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/payments/{payment.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Payment, payment.id) is not None  # не удалён


def test_delete_member_payment_rejects_payment_from_another_account(app, db, client):
    account = _setup_account(db, account_number="19504", garage_number="98", fee_type_code="membership_a")
    other_account = _setup_account(db, account_number="19505", garage_number="99", fee_type_code="membership_b")
    payment = Payment(account_id=other_account.id, date=dt.date(2026, 1, 1), amount=Decimal("300.00"))
    db.add(payment)
    make_user(db, "chair42", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair42", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account.id}/payments/{payment.id}/delete")
    assert resp.status_code == 404
    db.expire_all()
    assert database.db_session.get(Payment, payment.id) is not None
