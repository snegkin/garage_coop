"""
Выписка по лицевым счетам человека (/persons/<id>/statement) — упрощена по
просьбе пользователя: одна строка на счёт (сумма начислений/платежей за
всё время), без построчной разбивки по годам/статусам оплаты каждого
начисления. Пеня — отдельным блоком под основной таблицей (другая природа
баланса), с итоговой строкой «Баланс без пени» + «Пеня» = «Итого».
Номер счёта в каждой строке — ссылка на карточку счёта
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
    RoleEnum, FeeType, MemberAccount, Charge, Payment, PersonalAccount, Cooperative, KeyRate,
)
from app.i18n import fmt2
from app.accounting import reallocate_member_charges

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
    assert "Итого" in body

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
    assert "Итого" not in body  # блок пени/итога целиком не рендерится без пени
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


def test_statement_print_form_has_letterhead_and_signatures(app, db, client):
    coop = Cooperative(full_name='Гаражный кооператив "Заря"', inn="7701234567", kpp="770101001", ogrn="1027700123456", legal_address="г. Москва, ул. Гаражная, д. 1")
    db.add(coop)
    chairman = make_person(db, full_name="Председателев Пётр Петрович", is_chairman=True)
    accountant = make_person(db, full_name="Бухгалтерова Анна Ивановна", is_accountant=True)
    person = make_person(db, full_name="Обычный Человек")
    make_user(db, "board6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board6", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # официальная шапка слева вверху — логотип + реквизиты кооператива
    assert 'class="print-letterhead"' in body
    assert 'src="/static/logo.png"' in body
    assert "Гаражный кооператив" in body and "Заря" in body  # кавычки в HTML экранируются как &#34;
    assert "7701234567" in body
    assert "770101001" in body
    assert "г. Москва, ул. Гаражная, д. 1" in body

    # подписи и место под печать — справа внизу, ФИО сокращённо (Фамилия И.О.)
    assert 'class="print-signatures"' in body
    assert "Председатель" in body
    assert "Председателев П.П." in body
    assert "Председателев Пётр Петрович" not in body
    assert "Бухгалтер" in body
    assert "Бухгалтерова А.И." in body
    assert "Бухгалтерова Анна Ивановна" not in body
    assert 'class="stamp-place"' in body
    assert "М.П." in body


def test_statement_print_form_without_coop_or_officers_does_not_crash(app, db, client):
    """Реквизиты/председатель/бухгалтер не заполнены — форма всё равно рендерится,
    просто без этих данных (не 500)."""
    person = make_person(db, full_name="Без Реквизитов")
    make_user(db, "board7", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board7", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="print-letterhead"' in body
    assert 'class="print-signatures"' in body


# ---------------------------------------------------------------------------
# Расчёт пени (приложение для суда) — /persons/<id>/penalty-calculation.
# Пересчитывает пеню заново по дням, от начала просрочки до сегодня, с
# раскладкой по периодам действия ставки ЦБ РФ и знаменателя (1/300 первые
# 30 дней, 1/150 далее) — независимо от того, что и когда формально
# начислила бухгалтерия (accrue_penalties). Ссылка на страницу видна на
# выписке только правлению и только если по человеку УЖЕ есть начисленная
# пеня (penalty_rows) — расчёт готовится для суда, не информационная штука
# для рядового члена.
# ---------------------------------------------------------------------------

def _setup_coop_with_due_date_and_rate(db, day=1, month=6, rate="16.00"):
    coop = Cooperative(full_name='Гаражный кооператив "Заря"', inn="7701234567", kpp="770101001", ogrn="1027700123456", dues_due_day=day, dues_due_month=month)
    db.add(coop)
    db.add(KeyRate(effective_date=dt.date(2020, 1, 1), rate_percent=Decimal(rate)))
    return coop


def test_penalty_calculation_shows_breakdown_by_rate_period(app, db, client):
    _setup_coop_with_due_date_and_rate(db)
    chairman = make_person(db, full_name="Председателев Пётр Петрович", is_chairman=True)
    person = make_person(db, full_name="Должников Должник Должникович")
    garage = make_garage(db, number="60")
    make_ownership(db, garage, person)
    membership, _land_tax, _penalty = _setup_fee_types(db)

    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="16001")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))  # не оплачено вовсе

    make_user(db, "board8", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board8", "pass12345")

    resp = client.get(f"/persons/{person.id}/penalty-calculation")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "Должников Должник Должникович" in body
    assert "Членский взнос" in body
    # первые 30 дней просрочки — 1/300, дальше — 1/150 (ставка не менялась,
    # долг не гасился — ровно два периода)
    assert "1/300" in body
    assert "1/150" in body
    assert "Итого по начислению" in body
    assert "Итого пени" in body
    # официальная шапка и подпись председателя (сокращённо)
    assert 'class="print-letterhead"' in body
    assert "Заря" in body
    assert "Председателев П.П." in body


def test_penalty_calculation_empty_when_nothing_overdue(app, db, client):
    _setup_coop_with_due_date_and_rate(db)
    person = make_person(db, full_name="Без Долгов")
    garage = make_garage(db, number="61")
    make_ownership(db, garage, person)
    membership, _land_tax, _penalty = _setup_fee_types(db)

    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="16100")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("500.00")))
    db.add(Payment(account_id=account.id, date=dt.date(2025, 5, 1), amount=Decimal("500.00")))  # оплачено до срока
    db.flush()
    reallocate_member_charges(account)  # без этого ChargeAllocation не появится — платёж «не увидят»

    make_user(db, "board9", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board9", "pass12345")

    resp = client.get(f"/persons/{person.id}/penalty-calculation")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Просроченных начислений с непогашенным остатком не найдено." in body


def test_penalty_calculation_only_for_board(db, client):
    _setup_coop_with_due_date_and_rate(db)
    person = make_person(db, full_name="Рядовой Член")
    make_user(db, "member_pc", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member_pc", "pass12345")

    resp = client.get(f"/persons/{person.id}/penalty-calculation")
    assert resp.status_code == 302


def test_statement_link_to_penalty_calculation_shown_only_with_accrued_penalty(app, db, client):
    _setup_coop_with_due_date_and_rate(db)
    person = make_person(db, full_name="С Пеней")
    garage = make_garage(db, number="62")
    make_ownership(db, garage, person)
    membership, _land_tax, penalty = _setup_fee_types(db)

    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="16200")
    penalty_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П16200")
    db.add_all([account, penalty_account])
    db.flush()
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000.00")))
    db.add(Charge(account_id=penalty_account.id, year=2026, amount=Decimal("50.00")))  # уже начисленная пеня

    make_user(db, "board10", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "board10", "pass12345")

    resp = client.get(f"/persons/{person.id}/statement")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'href="/persons/{person.id}/penalty-calculation"' in body
    assert ">Расчёт<" in body
