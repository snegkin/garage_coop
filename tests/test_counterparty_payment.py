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
from app.models import RoleEnum, Counterparty, BankAccount, BankStatementLine, CounterpartyPayment, Expense

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


def make_statement_line(db, bank_account, direction="debit", amount=Decimal("500.00"), operation_date=dt.date(2026, 1, 10), **kwargs):
    line = BankStatementLine(
        bank_account_id=bank_account.id, direction=direction, amount=amount, operation_date=operation_date, **kwargs,
    )
    db.add(line)
    db.flush()
    return line


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


def test_add_payment_empty_amount_closes_debt_in_full(app, db, client):
    """Пустое поле суммы — как и на карточке лицевого счёта (finance.
    add_member_payment) — закрывает весь долг перед контрагентом целиком,
    без списания со счёта (adjust_balance не отмечен в этом запросе)."""
    counterparty = make_counterparty(db)
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("2500.00")))
    db.flush()
    make_user(db, "board58", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board58", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={"date": "2026-02-01", "amount": ""})
    assert resp.status_code == 302
    db.expire_all()

    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert payment.amount == Decimal("2500.00")


def test_add_payment_empty_amount_marks_expense_as_paid(app, db, client):
    """Регрессия: counterparty_balance() (вызывается для «закрыть весь долг»
    при пустой сумме) читает counterparty.payments/.expenses ДО того, как
    pay_counterparty() добавляет новый CounterpartyPayment через
    counterparty_id= напрямую (не через relationship-атрибут) — без
    expire() в reallocate_counterparty_expenses() эта уже закэшированная
    коллекция не увидела бы новый платёж, и ExpenseAllocation не создался
    бы вовсе: плашка «не оплачено» осталась бы висеть, хотя Payment на всю
    сумму долга реально был бы создан (см. предыдущий тест)."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, amount=Decimal("2500.00"), operation_date=dt.date(2026, 2, 1))
    expense = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("2500.00"))
    db.add(expense)
    db.flush()
    make_user(db, "board58b", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board58b", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-02-01", "amount": "",
        "bank_account_id": str(account.id), "adjust_balance": "on",
        "bank_statement_line_id": str(line.id),
    })
    assert resp.status_code == 302
    db.expire_all()

    from app.accounting import expense_paid_amount
    assert expense_paid_amount(expense) == Decimal("2500.00")

    resp = client.get(f"/counterparties/{counterparty.id}")
    html = resp.get_data(as_text=True)
    stats_start = html.find('id="expensesTable"')
    assert "оплачено</span>" in html[stats_start:stats_start + 2000]
    assert "не оплачено" not in html[stats_start:stats_start + 2000]


def test_add_payment_empty_amount_without_debt_is_rejected(app, db, client):
    counterparty = make_counterparty(db)
    make_user(db, "board59", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board59", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={"date": "2026-02-01", "amount": ""})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).count() == 0


def test_add_payment_form_shows_debt_amount_placeholder(app, db, client):
    counterparty = make_counterparty(db)
    db.add(Expense(counterparty_id=counterparty.id, date=dt.date(2026, 1, 1), amount=Decimal("1234.56")))
    db.flush()
    make_user(db, "board60", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board60", "pass12345")

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    assert 'placeholder="1234,56"' in resp.get_data(as_text=True)


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


# ---------------------------------------------------------------------------
# Ссылка на строку выписки банка вместо прикрепления платёжного поручения
# ---------------------------------------------------------------------------

def test_statement_line_dropdown_shows_bank_and_account_id_for_js(app, db, client):
    """При нескольких банковских счетах в списке "Строка выписки банка"
    нужно видеть, какому счёту принадлежит строка (иначе непонятно, какие
    строки чьи) — и data-bank-account-id, по которому JS в detail.html
    подставляет "Счёт списания" автоматически при выборе строки."""
    counterparty = make_counterparty(db)
    account_a = make_bank_account(db, bank_name="Сбербанк", checking_account="40703810000000000001")
    account_b = make_bank_account(db, bank_name="Тинькофф", checking_account="40703810000000000002")
    line_a = make_statement_line(db, account_a, amount=Decimal("500.00"))
    line_b = make_statement_line(db, account_b, amount=Decimal("700.00"))
    make_user(db, "board60b", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board60b", "pass12345")

    resp = client.get(f"/counterparties/{counterparty.id}")
    html = resp.get_data(as_text=True)
    assert "Сбербанк (40703810000000000001)" in html
    assert "Тинькофф (40703810000000000002)" in html
    assert f'data-bank-account-id="{account_a.id}"' in html
    assert f'data-bank-account-id="{account_b.id}"' in html
    assert "data-account-lock-hint" in html


def test_statement_line_dropdown_exposes_amount_and_inn_for_autoselect_js(app, db, client):
    """data-amount/data-counterparty-inn на каждом <option> — по ним JS в
    detail.html (addPaymentModal 'shown.bs.modal') пытается сам выбрать
    подходящую строку выписки при открытии формы нового платежа."""
    counterparty = make_counterparty(db, inn="7701234567")
    account = make_bank_account(db)
    line = make_statement_line(db, account, amount=Decimal("500.00"), counterparty_inn="7701234567")
    make_user(db, "board60c", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board60c", "pass12345")

    resp = client.get(f"/counterparties/{counterparty.id}")
    html = resp.get_data(as_text=True)
    assert 'data-amount="500.00"' in html
    assert 'data-counterparty-inn="7701234567"' in html
    assert "counterpartyInn = \"7701234567\"" in html


def test_add_payment_references_debit_statement_line(app, db, client):
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="debit", amount=Decimal("500.00"))
    make_user(db, "board61", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board61", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    assert resp.status_code == 302
    db.expire_all()

    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()
    assert payment.bank_statement_line_id == line.id
    assert payment.document_id is None


def test_add_payment_rejects_credit_statement_line(app, db, client):
    """Сослаться можно только на списание — зачисление (деньги, пришедшие НА
    счёт) не может быть подтверждением платежа, ушедшего контрагенту."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="credit", amount=Decimal("500.00"))
    make_user(db, "board62", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board62", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).count() == 0


def test_add_payment_rejects_already_referenced_statement_line(app, db, client):
    counterparty_a = make_counterparty(db, name="ООО Первый")
    counterparty_b = make_counterparty(db, name="ООО Второй")
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="debit", amount=Decimal("500.00"))
    make_user(db, "board63", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board63", "pass12345")

    client.post(f"/counterparties/{counterparty_a.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    db.expire_all()
    assert db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty_a.id).one().bank_statement_line_id == line.id

    resp = client.post(f"/counterparties/{counterparty_b.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty_b.id).count() == 0


def test_reversed_payment_frees_its_referenced_statement_line(app, db, client):
    """Сторно платежа должно освобождать строку выписки, на которую он
    ссылался, для повторной привязки — иначе ошибочный/неверно заведённый
    платёж навсегда «занимает» строку выписки, хотя сам он уже недействует
    (см. app/counterparties.py:_resolve_statement_line)."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="debit", amount=Decimal("500.00"))
    make_user(db, "board66", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board66", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()

    client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/reverse", data={"date": "2026-01-11"})
    db.expire_all()

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert f'value="{line.id}"' in resp.get_data(as_text=True)  # снова доступна в списке

    resp = client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-12", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    assert resp.status_code == 302
    db.expire_all()
    new_payments = (
        db.query(CounterpartyPayment)
        .filter_by(counterparty_id=counterparty.id, bank_statement_line_id=line.id, reverses_payment_id=None)
        .all()
    )
    assert len(new_payments) == 2  # исходный (сторнированный) + новый, оба всё ещё ссылаются на строку


def test_edit_payment_can_keep_its_own_referenced_statement_line(app, db, client):
    """Повторное сохранение формы правки с той же строкой выписки — не
    должно упереться в «уже привязана к другому платежу» (это тот же самый
    платёж)."""
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="debit", amount=Decimal("500.00"))
    make_user(db, "board64", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board64", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()

    resp = client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/edit", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(CounterpartyPayment, payment.id).bank_statement_line_id == line.id


def test_edit_payment_can_clear_referenced_statement_line(app, db, client):
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="debit", amount=Decimal("500.00"))
    make_user(db, "board65", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board65", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })
    db.expire_all()
    payment = db.query(CounterpartyPayment).filter_by(counterparty_id=counterparty.id).one()

    resp = client.post(f"/counterparties/{counterparty.id}/payments/{payment.id}/edit", data={
        "date": "2026-01-10", "amount": "500.00",
    })
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(CounterpartyPayment, payment.id).bank_statement_line_id is None


def test_payments_table_links_to_referenced_statement_line(app, db, client):
    counterparty = make_counterparty(db)
    account = make_bank_account(db)
    line = make_statement_line(db, account, direction="debit", amount=Decimal("500.00"))
    make_user(db, "board66", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board66", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/payments/new", data={
        "date": "2026-01-10", "amount": "500.00", "bank_statement_line_id": str(line.id),
    })

    resp = client.get(f"/counterparties/{counterparty.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "по выписке" in body
    assert f"#stmt-line-{line.id}" in body
