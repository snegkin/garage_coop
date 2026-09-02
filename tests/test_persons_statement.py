"""
Выписка по лицевым счетам человека (/persons/<id>/statement) — упрощена по
просьбе пользователя: одна строка на счёт (сумма начислений/платежей за
всё время), без построчной разбивки по годам/статусам оплаты каждого
начисления. Пеня — отдельным блоком под основной таблицей (другая природа
баланса), с итоговой строкой «Баланс без пени» + «Пеня» = «Итого/
Задолженность». Номер счёта в каждой строке — ссылка на карточку счёта
(finance.member_account_detail для взносов/налога, garages.detail для
электричества — своей страницы у него нет).

Печать — отдельная форма (.statement-print, скрыта на экране), не
распечатка экранных bootstrap-таблиц (.statement-screen, скрыта на
печати) — та же идея, что и у pd4/print.html: свой шрифт/границы/@page,
не завязана на тему сайта. Обе версии рендерятся сервером всегда, видимость
переключает только CSS @media print — эти тесты проверяют, что в HTML
вообще есть обе структуры со своей разметкой (саму печать браузера тестами
не проверить).
"""
import datetime as dt
from decimal import Decimal

from app.models import (
    RoleEnum, FeeType, MemberAccount, Charge, Payment, PersonalAccount,
)
from app.i18n import fmt2

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_fee_types(db):
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    land_tax = FeeType(code="land_tax", name="Земельный налог", type_code="2")
    penalty = FeeType(code="membership_penalty", name="Пеня по взносу", type_code="1", is_penalty=True)
    db.add_all([membership, land_tax, penalty])
    db.flush()
    return membership, land_tax, penalty


def test_statement_shows_one_row_per_account_with_totals_and_links(app, db, client):
    person = make_person(db, full_name="Тестовый Человек")
    garage = make_garage(db, number="50")
    make_ownership(db, garage, person)
    membership, land_tax, penalty = _setup_fee_types(db)

    acc1 = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="15001")
    acc2 = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="15002")
    acc_penalty = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П15001")
    db.add_all([acc1, acc2, acc_penalty])
    db.flush()

    db.add(Charge(account_id=acc1.id, year=2020, amount=Decimal("1000.00")))
    db.add(Payment(account_id=acc1.id, date=dt.date(2020, 6, 1), amount=Decimal("1000.00")))
    db.add(Charge(account_id=acc2.id, year=2026, amount=Decimal("1200.00")))
    db.add(Payment(account_id=acc2.id, date=dt.date(2026, 1, 1), amount=Decimal("800.00")))
    db.add(Charge(account_id=acc_penalty.id, year=2026, amount=Decimal("85.00")))

    elec_account = PersonalAccount(garage_id=garage.id, account_number="050")
    db.add(elec_account)
    db.add(Charge(garage_id=garage.id, year=2025, amount=Decimal("500.00")))
    db.add(Payment(garage_id=garage.id, date=dt.date(2025, 6, 1), amount=Decimal("500.00")))

    make_user(db, "board1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # заголовок — диапазон лет по всем начислениям (2020..2026)
    assert "с 2020 по 2026 г." in body

    # каждый счёт — своя строка, номер счёта — ссылка на карточку
    assert f'<a href="/finance/member-accounts/{acc1.id}">15001</a>' in body
    assert f'<a href="/finance/member-accounts/{acc2.id}">15002</a>' in body
    assert f'<a href="/garages/{garage.id}">050</a>' in body
    assert "Членский взнос, гараж №50" in body
    assert "Земельный налог, гараж №50" in body
    assert "Электричество, гараж №50" in body

    # пеня — отдельная строка/блок, не в основной таблице
    assert f'<a href="/finance/member-accounts/{acc_penalty.id}">П15001</a>' in body
    assert "Пеня по взносу" in body

    # суммы: баланс без пени = 0 (взнос) + (-400) (налог) + 0 (эл-во) = -400
    assert fmt2(Decimal("-400.00")) in body
    # пеня = -85
    assert fmt2(Decimal("-85.00")) in body
    # итого = -485
    assert fmt2(Decimal("-485.00")) in body
    assert "Задолженность" in body

    # старой построчной разбивки по годам/статусам оплаты больше нет
    assert "не оплачено" not in body
    assert "частично" not in body
    assert "оплачено" not in body


def test_statement_without_penalty_has_no_penalty_block(app, db, client):
    person = make_person(db, full_name="Без Пени")
    garage = make_garage(db, number="51")
    make_ownership(db, garage, person)
    membership, _land_tax, _penalty = _setup_fee_types(db)

    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="15100")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("500.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2026, 1, 1), amount=Decimal("500.00")))

    make_user(db, "board2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board2", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # "Пеня" в меню навигации есть всегда (ссылка на /finance/penalty) —
    # проверяем специфичные для блока пени в самой выписке признаки
    assert "Пеня по взносу" not in body
    assert "Задолженность" not in body
    assert "за 2026 г." in body  # год_from == год_to — единственный год


def test_statement_empty_state(app, db, client):
    person = make_person(db, full_name="Без Счетов")
    make_user(db, "board3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board3", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "У этого человека пока нет лицевых счетов." in body


def test_statement_self_view_allowed_others_forbidden(app, db, client):
    person = make_person(db, full_name="Сам Себя")
    other = make_person(db, full_name="Посторонний")
    make_user(db, "self_user", "pass12345", role=RoleEnum.MEMBER, person=person)
    make_user(db, "other_user", "pass12345", role=RoleEnum.MEMBER, person=other)
    db.commit()

    login(client, "self_user", "pass12345")
    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200

    client.get("/auth/logout")
    login(client, "other_user", "pass12345")
    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 403


def test_statement_has_separate_print_form_reset_from_site_styles(app, db, client):
    person = make_person(db, full_name="Печатная Форма")
    garage = make_garage(db, number="52")
    make_ownership(db, garage, person)
    membership, _land_tax, penalty = _setup_fee_types(db)

    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="15200")
    penalty_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П15200")
    db.add_all([account, penalty_account])
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("500.00")))
    db.add(Charge(account_id=penalty_account.id, year=2026, amount=Decimal("20.00")))

    make_user(db, "board5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board5", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # своя печатная форма — сброс шрифта/полей, не завязана на тему сайта
    assert '@page { size: auto; margin: 15mm; }' in body
    assert 'font-family: Arial, Helvetica, sans-serif;' in body
    assert '.statement-print {' in body
    assert '.statement-screen' in body and 'display: none !important;' in body  # скрыта именно на печати

    # данные есть в печатной форме, но БЕЗ ссылок (печатному листу некуда вести)
    assert 'class="print-table"' in body
    assert '<td>15200</td>' in body
    assert '<td>П15200</td>' in body
    assert '<a href="/finance/member-accounts/' in body  # ссылка всё ещё есть в экранной версии
