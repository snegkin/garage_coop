"""
Сводка «Оплата взносов по гаражам» (/finance/dues-by-garage,
app/finance.py: _dues_by_garage/_unmatched_statement_credits) — по каждому
гаражу и виду взноса за выбранный год начислено/погашено по счетам членов
(вместе с архивными счетами прежних собственников), плюс неразнесённые
зачисления из выписок (без организаций и контрагентов) для поиска
гаражей-кандидатов по сумме.
"""
import datetime as dt
from decimal import Decimal

from app.accounting import reallocate_member_charges
from app.finance import _dues_by_garage, _dues_years, _unmatched_statement_credits
from app.models import (
    RoleEnum, FeeType, MemberAccount, Charge, Payment, BankAccount, BankStatementLine, Phone, Counterparty,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _fee_type(db, code="membership", name="Членский взнос", **kwargs):
    ft = FeeType(code=code, name=name, **kwargs)
    db.add(ft)
    db.flush()
    return ft


def _account(db, person, garage, fee_type, number, **kwargs):
    acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=number, **kwargs)
    db.add(acc)
    db.flush()
    return acc


def _row(summary, garage):
    return next(r for r in summary["rows"] if r["garage"].id == garage.id)


def test_cells_statuses_by_year(db):
    membership = _fee_type(db)
    person = make_person(db, full_name="Иванов Иван")
    garage = make_garage(db, number="12")
    make_ownership(db, garage, person)
    acc = _account(db, person, garage, membership, "M12")
    db.add(Charge(account_id=acc.id, year=2024, amount=Decimal("1000.00")))
    db.add(Charge(account_id=acc.id, year=2025, amount=Decimal("1000.00")))
    db.add(Charge(account_id=acc.id, year=2026, amount=Decimal("1000.00")))
    db.add(Payment(account_id=acc.id, date=dt.date(2025, 2, 1), amount=Decimal("1400.00")))
    db.flush()
    reallocate_member_charges(acc)
    db.commit()

    assert _dues_years() == [2024, 2025, 2026]
    # FIFO — первым гасится самый старый год
    assert _row(_dues_by_garage(2024), garage)["cells"][membership.id]["status"] == "paid"
    cell_2025 = _row(_dues_by_garage(2025), garage)["cells"][membership.id]
    assert cell_2025["status"] == "partial"
    assert cell_2025["debt"] == Decimal("600.00")
    assert _row(_dues_by_garage(2026), garage)["total"]["status"] == "unpaid"


def test_archived_previous_owner_accounts_count_for_garage(db):
    """Прошлый год оплачен прежним собственником (его счёт уже архивный) —
    гараж за этот год всё равно оплачен."""
    membership = _fee_type(db)
    old_owner = make_person(db, full_name="Прежний Собственник")
    new_owner = make_person(db, full_name="Новый Собственник")
    garage = make_garage(db, number="13")
    make_ownership(db, garage, new_owner)
    old_acc = _account(db, old_owner, garage, membership, "M13", is_archived=True)
    db.add(Charge(account_id=old_acc.id, year=2025, amount=Decimal("500.00")))
    db.add(Payment(account_id=old_acc.id, date=dt.date(2025, 3, 1), amount=Decimal("500.00")))
    db.flush()
    reallocate_member_charges(old_acc)
    db.commit()

    row = _row(_dues_by_garage(2025), garage)
    assert row["cells"][membership.id]["status"] == "paid"
    assert [p.full_name for p in row["owners"]] == ["Новый Собственник"]


def test_columns_by_fee_type_penalty_excluded(db):
    membership = _fee_type(db)
    land_tax = _fee_type(db, code="land_tax", name="Земельный налог")
    penalty = _fee_type(db, code="membership_penalty", name="Пеня", is_penalty=True)
    person = make_person(db, full_name="Фильтр Видов")
    garage = make_garage(db, number="14")
    make_ownership(db, garage, person)
    for ft, number, amount in ((membership, "M14", "100.00"), (land_tax, "L14", "30.00"), (penalty, "P14", "7.00")):
        acc = _account(db, person, garage, ft, number)
        db.add(Charge(account_id=acc.id, year=2025, amount=Decimal(amount)))
    db.commit()

    summary = _dues_by_garage(2025)
    assert [ft.name for ft in summary["fee_types"]] == ["Земельный налог", "Членский взнос"]
    row = _row(summary, garage)
    assert row["cells"][land_tax.id]["charged"] == Decimal("30.00")
    assert row["total"]["charged"] == Decimal("130.00")


def test_garages_sorted_numerically(db):
    _fee_type(db)
    for number in ("10", "2", "1а"):
        make_garage(db, number=number)
    db.commit()
    assert [r["garage"].number for r in _dues_by_garage(2025)["rows"]] == ["1а", "2", "10"]


def test_unmatched_statement_credits(db):
    bank = BankAccount(bank_name="Сбербанк", checking_account="40703810000000000001", balance=Decimal("0"))
    db.add(bank)
    db.flush()
    unmatched = BankStatementLine(
        bank_account_id=bank.id, direction="credit", amount=Decimal("1000.00"),
        operation_date=dt.date(2026, 3, 1), counterparty_name="Посторонний П.П.",
        payment_purpose="Членские взносы за гараж",
    )
    unmatched.counterparty_inn = "761002318180"
    debit = BankStatementLine(
        bank_account_id=bank.id, direction="debit", amount=Decimal("50.00"), operation_date=dt.date(2026, 3, 2),
    )
    # возврат от организации (ИНН юрлица — 10 цифр) — не взнос члена
    refund = BankStatementLine(
        bank_account_id=bank.id, direction="credit", amount=Decimal("10287.00"), operation_date=dt.date(2025, 7, 31),
        counterparty_name='ООО "ИНТЕРНЕТ РЕШЕНИЯ"', counterparty_inn="7704217370",
    )
    # ИП из справочника контрагентов (ИНН 12 цифр, как у физлица)
    db.add(Counterparty(name="ИП Подрядчиков", inn="500100732259"))
    from_counterparty = BankStatementLine(
        bank_account_id=bank.id, direction="credit", amount=Decimal("300.00"), operation_date=dt.date(2026, 4, 1),
        counterparty_name="ИП Подрядчиков", counterparty_inn="500100732259",
    )
    db.add_all([unmatched, debit, refund, from_counterparty])
    db.commit()

    assert _unmatched_statement_credits() == [unmatched]


def test_page_renders_for_board(db, client):
    membership = _fee_type(db)
    person = make_person(db, full_name="Должник Звонков")
    db.add(Phone(person_id=person.id, number="+7 900 123-45-67"))
    garage = make_garage(db, number="15")
    make_ownership(db, garage, person)
    acc = _account(db, person, garage, membership, "M15")
    db.add(Charge(account_id=acc.id, year=2025, amount=Decimal("1000.00")))
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/finance/dues-by-garage")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Должник Звонков" in body
    assert 'href="tel:+79001234567"' in body
    assert "dues-unpaid" in body
    assert "1000,00" in body


def test_page_requires_board(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    assert client.get("/finance/dues-by-garage").status_code == 302
