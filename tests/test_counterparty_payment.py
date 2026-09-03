"""
Платежи контрагенту (app/counterparties.py add_payment/edit_payment/
reverse_payment, ядро — app/accounting.py pay_counterparty/
edit_counterparty_payment/reverse_counterparty_payment).

Отдельный фокус — CounterpartyPayment.adjusts_bank_balance: платёж можно
внести задним числом только для истории (деньги реально списались со счёта
раньше, до появления записи в системе) — тогда баланс BankAccount менять не
нужно, хотя сам счёт списания по-прежнему можно указать для отчётности.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import RoleEnum, Counterparty, BankAccount, CounterpartyPayment, Expense

from tests.conftest import make_user, login


def make_counterparty(db, name="ООО Ромашка", **kwargs):
    c = Counterparty(name=name, **kwargs)
    db.add(c)
    db.flush()
    return c


def make_bank_account(db, bank_name="Сбербанк", checking_account="40703810000000000001", balance=Decimal("10000.00")):
    acc = BankAccount(bank_name=bank_name, checking_account=checking_account, balance=balance)
    db.add(acc)
    db.flush()
    return acc


def test_add_payment_with_default_checkbox_deducts_balance(app, db, client):
    """Чекбокс «Списать сумму со счёта сейчас» по умолчанию включён (checked
    в HTML) — обычный случай, ничего не меняя, баланс списывается как
    раньше."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board50", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board50", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-15", "amount": "1500.00", "bank_account_id": str(account.id),
        "adjust_balance": "on",
    })
    assert resp.status_code == 302
    db.expire_all()

    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert payment.adjusts_bank_balance is True
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("8500.00")


def test_add_payment_backdated_does_not_touch_balance(app, db, client):
    """Чекбокс снят (форма его просто не отправляет) — деньги были списаны
    раньше, до появления этой записи в системе, повторно уменьшать баланс
    не нужно."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board51", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board51", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2025-06-01", "amount": "1500.00", "bank_account_id": str(account.id),
        # "adjust_balance" отсутствует — как непроставленный чекбокс в браузере
    })
    assert resp.status_code == 302
    db.expire_all()

    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert payment.adjusts_bank_balance is False
    assert payment.bank_account_id == account.id  # счёт для отчётности всё равно сохранён
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("10000.00")  # не изменился


def test_add_payment_without_bank_account_ignores_flag(app, db, client):
    counterparty = make_counterparty(db)
    make_user(db, "board52", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board52", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-15", "amount": "500.00", "adjust_balance": "on",
    })
    assert resp.status_code == 302
    db.expire_all()

    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert payment.bank_account_id is None
    assert payment.adjusts_bank_balance is False  # нет счёта — списывать физически нечего


def test_edit_payment_from_backdated_to_adjusting_deducts_once(app, db, client):
    """Правка «задним числом» платежа на «списать сейчас» — должна СПИСАТЬ
    сумму (раньше не списывала), а не сначала неверно вернуть деньги,
    которые на самом деле никогда не вычитались."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board53", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board53", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2025-06-01", "amount": "1000.00", "bank_account_id": str(account.id),
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("10000.00")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/edit", data={
        "date": "2025-06-01", "amount": "1000.00", "bank_account_id": str(account.id),
        "adjust_balance": "on",
    })
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(CounterpartyPayment, payment.id).adjusts_bank_balance is True
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("9000.00")


def test_edit_payment_from_adjusting_to_backdated_refunds_once(app, db, client):
    """Обратная правка — «списать сейчас» на «задним числом»: должна ВЕРНУТЬ
    списанную сумму (раз теперь считаем, что списывать не нужно) и больше
    её не трогать."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board54", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board54", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-15", "amount": "1000.00", "bank_account_id": str(account.id),
        "adjust_balance": "on",
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("9000.00")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/edit", data={
        "date": "2026-01-15", "amount": "1000.00", "bank_account_id": str(account.id),
        # без adjust_balance
    })
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(CounterpartyPayment, payment.id).adjusts_bank_balance is False
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("10000.00")


def test_reverse_backdated_payment_does_not_touch_balance(app, db, client):
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board55", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board55", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2025-06-01", "amount": "700.00", "bank_account_id": str(account.id),
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("10000.00")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/reverse", data={"date": "2026-01-01"})
    assert resp.status_code == 302
    db.expire_all()

    reversal = db.query(CounterpartyPayment).filter_by(reverses_payment_id=payment.id).one()
    assert reversal.adjusts_bank_balance is False
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("10000.00")  # не изменился


def test_reverse_adjusting_payment_refunds_balance(app, db, client):
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board56", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board56", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-15", "amount": "700.00", "bank_account_id": str(account.id),
        "adjust_balance": "on",
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("9300.00")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/reverse", data={"date": "2026-01-16"})
    assert resp.status_code == 302
    db.expire_all()

    reversal = db.query(CounterpartyPayment).filter_by(reverses_payment_id=payment.id).one()
    assert reversal.adjusts_bank_balance is True
    assert database.db_session.get(BankAccount, account.id).balance == Decimal("10000.00")


def test_payments_table_marks_backdated_entries(app, db, client):
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    make_user(db, "board57", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board57", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2025-06-01", "amount": "300.00", "bank_account_id": str(account.id),
    })

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    assert "задним числом" in resp.get_data(as_text=True)
