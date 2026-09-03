"""
Блок статистики по видам счетов под таблицей на /finance/member-accounts
(app/finance.py:_account_stats) — число счетов, число с долгом, сумма долга
и итоговый баланс по каждому виду взноса + строка "Итого".

Считается в Python из уже вычисленных балансов (rows), без отдельных
SQL-запросов — балансы и так нужны для самой таблицы. Архивные счета
(закрытые при смене собственника гаража) в статистику не входят — тот же
смысл "долга", что и на дашборде (см. test_dashboard.py).
"""
import datetime as dt
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _board_user(db, username="board1"):
    make_user(db, username, "pass1234", role=RoleEnum.BOARD)
    db.commit()


def test_stats_block_absent_without_accounts(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    assert "Статистика по видам счетов" not in resp.get_data(as_text=True)


def test_stats_grouped_by_fee_type_and_totals(db, client):
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    land_tax = FeeType(code="land_tax", name="Земельный налог", type_code="2")
    db.add_all([membership, land_tax])
    db.flush()

    person1 = make_person(db, full_name="Должник Один")
    garage1 = make_garage(db, number="401")
    make_ownership(db, garage1, person1)
    acc1 = MemberAccount(person_id=person1.id, garage_id=garage1.id, fee_type_id=membership.id, account_number="14001")

    person2 = make_person(db, full_name="Плательщик Два")
    garage2 = make_garage(db, number="402")
    make_ownership(db, garage2, person2)
    acc2 = MemberAccount(person_id=person2.id, garage_id=garage2.id, fee_type_id=membership.id, account_number="14002")

    acc3 = MemberAccount(person_id=person1.id, garage_id=garage1.id, fee_type_id=land_tax.id, account_number="14003")

    db.add_all([acc1, acc2, acc3])
    db.flush()

    # членский взнос: acc1 в долгу на 100, acc2 переплачен на 50
    db.add(Charge(account_id=acc1.id, year=2026, amount=Decimal("100.00")))
    db.add(Charge(account_id=acc2.id, year=2026, amount=Decimal("100.00")))
    db.add(Payment(account_id=acc2.id, date=dt.date(2026, 1, 1), amount=Decimal("150.00")))
    # земельный налог: acc3 полностью оплачен, долга нет
    db.add(Charge(account_id=acc3.id, year=2026, amount=Decimal("30.00")))
    db.add(Payment(account_id=acc3.id, date=dt.date(2026, 1, 1), amount=Decimal("30.00")))

    _board_user(db)
    login(client, "board1", "pass1234")
    db.commit()

    resp = client.get("/finance/member-accounts")
    html = resp.get_data(as_text=True)
    assert "Статистика по видам счетов" in html
    stats_html = html.split("Статистика по видам счетов")[1]

    membership_block = stats_html.split("Членский взнос")[1].split("</tr>")[0]
    assert ">2<" in membership_block  # 2 счёта
    assert ">1<" in membership_block  # 1 из них с долгом
    assert "100,00" in membership_block  # долг
    assert "50,00" in membership_block  # итоговый баланс: -100 (acc1) + 50 (acc2) = -50

    land_tax_block = stats_html.split("Земельный налог")[1].split("</tr>")[0]
    assert ">1<" in land_tax_block
    assert ">0<" in land_tax_block  # ни одного счёта с долгом

    # итог: 1 счёт с долгом (100), итоговый баланс -100 + 50 + 0 = -50
    assert "Итого" in stats_html


def test_archived_accounts_excluded_from_stats(db, client):
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    db.add(membership)
    db.flush()

    person = make_person(db, full_name="Бывший Собственник")
    garage = make_garage(db, number="403")
    make_ownership(db, garage, person)
    acc = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=membership.id,
        account_number="14004", is_archived=True,
    )
    db.add(acc)
    db.flush()
    db.add(Charge(account_id=acc.id, year=2026, amount=Decimal("500.00")))

    _board_user(db)
    login(client, "board1", "pass1234")
    db.commit()

    resp = client.get("/finance/member-accounts")
    html = resp.get_data(as_text=True)
    # архивный счёт — единственный в базе, значит статистика не показывается вовсе
    assert "Статистика по видам счетов" not in html
