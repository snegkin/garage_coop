"""
Добавление платежа (finance.add_member_payment) — поле суммы теперь
можно оставить пустым: если на счёте есть долг (баланс < 0), пустое
поле закрывает его полностью (см. placeholder в форме,
member_account_detail.html, показывающий именно эту сумму). Если долга
нет — пустое поле ничего не значит, сумму нужно указать явно.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import balance
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_account(db, account_number="19601", garage_number="96"):
    person = make_person(db, full_name="Долгов Дмитрий Долгович")
    garage = make_garage(db, number=garage_number)
    make_ownership(db, garage, person)
    fee_type = FeeType(code="membership", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=account_number)
    db.add(account)
    db.flush()
    return account


def test_empty_amount_closes_debt_in_full(app, db, client):
    account = _setup_account(db)
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("1710.00")))
    make_user(db, "board30", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board30", "pass12345")

    assert balance(account) == Decimal("-1710.00")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/payments/add",
        data={"date": "2026-01-15", "amount": ""},
    )
    assert resp.status_code == 302
    db.expire_all()

    assert balance(account) == Decimal("0.00")
    payment = db.query(Payment).filter_by(account_id=account.id).one()
    assert payment.amount == Decimal("1710.00")


def test_empty_amount_without_debt_is_rejected(app, db, client):
    account = _setup_account(db)
    make_user(db, "board31", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board31", "pass12345")

    assert balance(account) == Decimal("0.00")  # нет ни начислений, ни платежей — долга нет

    resp = client.post(
        f"/finance/member-accounts/{account.id}/payments/add",
        data={"date": "2026-01-15", "amount": ""},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is False
    db.expire_all()
    assert db.query(Payment).filter_by(account_id=account.id).count() == 0


def test_explicit_amount_still_works(app, db, client):
    """Регрессия: явно указанная сумма продолжает работать как раньше,
    независимо от того, есть долг или нет."""
    account = _setup_account(db)
    make_user(db, "board32", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board32", "pass12345")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/payments/add",
        data={"date": "2026-01-15", "amount": "500.00"},
    )
    assert resp.status_code == 302
    db.expire_all()
    assert balance(account) == Decimal("500.00")


def test_payment_form_placeholder_shows_debt_amount(app, db, client):
    """Плейсхолдер поля суммы — сумма долга, когда баланс отрицательный."""
    account = _setup_account(db)
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("1710.00")))
    make_user(db, "board33", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board33", "pass12345")

    resp = client.get(f"/finance/member-accounts/{account.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'placeholder="1710,00"' in body or 'placeholder="1710.00"' in body
