"""
«Финансы за год» (app/year_report.py): разбор CSV сметы и отчёта по
тратам, статьи расходов, заведение расходов по выписке банка, импорт
отчёта по тратам с сопоставлением с выпиской, доступ только председателю.
"""
import datetime as dt
import io
from decimal import Decimal

from app.models import (
    BankAccount, BankStatementLine, BudgetItem, Counterparty, CounterpartyPayment, Expense, RoleEnum,
)
from app.year_report import (
    CASH_COUNTERPARTY_NAME, _fit_to_payment, bill_date, guess_category, normalize_category,
    parse_budget_csv, parse_money, parse_spending_csv,
)
from tests.conftest import login, make_user

BUDGET_CSV = """Наименование,Кол-во,Цена,Сумма
Содержание кооператива,,,
Фонд оплаты труда,12,"2 000,00"," р. 24 000,00 "
Механизированная уборка снега,6,"2 500,00"," р. 15 000,00 "
,,," р. 39 000,00 "
,,,
Прочее,,,
"Резерв на оплату электроэнергии",,"35 000,00"," р. 35 000,00 "
,,," р. 35 000,00 "
Количество гаражей:,,,96
Целевая сумма:,,," р. 74 000,00 "
"""

SPENDING_CSV = """Наименование (канцелярия),,Дата,Кол-во, Цена,Сумма
"Бумага офисная, А4",,21.01.2026,1," р. 342,00 "," р. 342,00 "
,,,, Итого," р. 342,00 "
Наименование (электроэнергия),Месяц,Дата,"Кол-во, кВт", Цена,Сумма
"644,95",янв,13.02.2026,725," р. 5,66 "," р. 4 103,50 "
,авг,15.09.2026,0, р. -   , р. -
,,,, Итого," р. 4 103,50 "
Наименование (видеонаблюдение),,Дата,Кол-во, Цена,Сумма
Wi-Fi мост,0161382282-0005,29.07.2026,3," р. 3 653,00 "," р. 10 959,00 "
Кабель витая пара,0161382282-0005,30.07.2026,1," р. 400,00 "," р. 400,00 "
,,,, Итого," р. 11 359,00 "
Наименование (хоз. работы),,Дата,Кол-во, Цена,Сумма
Механизированная уборка снега,,20.01.2026,2," р. 2 500,00 "," р. 5 000,00 "
"Субботник, силами членов кооператива",,25.04.2026,4, р. -   , р. -
Легенда тип операции,,Озон,,,
,,,,Общая сумма:," р. 20 804,50 "
"""


def _chairman(db, client):
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")


def _bank(db):
    account = BankAccount(bank_name="Сбербанк", bik="043304609", checking_account="40703810777030002079",
                          is_primary=True, bank_fee_percent=Decimal("1.6"))
    db.add(account)
    db.flush()
    return account


def _debit(db, account, date, amount, name, inn, purpose, uid, counterparty_account=None):
    line = BankStatementLine(
        bank_account_id=account.id, external_uid=uid, operation_date=date, direction="debit",
        amount=Decimal(amount), counterparty_name=name, counterparty_inn=inn, payment_purpose=purpose,
        counterparty_account=counterparty_account,
    )
    db.add(line)
    db.flush()
    return line


# --- Разбор CSV -----------------------------------------------------------

def test_parse_money_handles_report_formats():
    assert parse_money(" р. 4 103,50 ") == Decimal("4103.50")
    assert parse_money("2 500,00") == Decimal("2500.00")
    assert parse_money(" р. -   ") is None
    assert parse_money("") is None


def test_parse_budget_csv_sections_and_items():
    items = parse_budget_csv(BUDGET_CSV)
    assert [(i["section"], i["name"], i["amount"]) for i in items] == [
        ("Содержание кооператива", "Фонд оплаты труда", Decimal("24000.00")),
        ("Содержание кооператива", "Механизированная уборка снега", Decimal("15000.00")),
        ("Прочее", "Резерв на оплату электроэнергии", Decimal("35000.00")),
    ]
    assert items[0]["quantity"] == Decimal("12") and items[0]["price"] == Decimal("2000.00")


def test_parse_spending_csv_rows_dates_and_categories():
    rows = parse_spending_csv(SPENDING_CSV)
    assert sum(r["amount"] for r in rows) == Decimal("20804.50")
    by_name = {r["name"]: r for r in rows}
    # Электроэнергия — дата траты = конец месяца потребления, день оплаты отдельно.
    electricity = by_name["Электроэнергия за янв 2026"]
    assert electricity["date"] == dt.date(2026, 1, 31)
    assert electricity["paid_on"] == dt.date(2026, 2, 13)
    assert electricity["category"] == "Электроэнергия"
    # Номер заказа во втором столбце.
    assert by_name["Wi-Fi мост"]["order"] == "0161382282-0005"
    assert by_name["Wi-Fi мост"]["category"] == "Видеонаблюдение"
    assert by_name["Механизированная уборка снега"]["category"] == "Хоз. работы"
    # Субботник с нулевой суммой и строки после «Легенды» не читаются.
    assert not any("Субботник" in r["name"] for r in rows)


def test_categories_from_aliases_and_keywords():
    assert normalize_category("электротовары") == "Электрика"
    assert normalize_category("Хостинг") == "Сайт и связь"
    assert normalize_category("штраф") == "Налоги, штрафы, пени"
    assert normalize_category(None) == "Прочее"
    # Раздел отчёта важнее ключевых слов: щит в разделе видеонаблюдения.
    assert guess_category("Щит распределительный навесной", "видеонаблюдение") == "Видеонаблюдение"
    assert guess_category("Земельный налог, казначейство России", "разное") == "Земельный налог"
    # «защитный» — не «щит», «фотореле» — не «ФОТ».
    assert guess_category("Диод защитный однонаправленный", "прочее") == "Прочее"
    assert guess_category("Фотореле IEK", None) != "Оплата труда"


def test_fit_to_payment_keeps_total_equal_to_payment():
    rows = [{"amount": Decimal("572.38")}, {"amount": Decimal("2388.00")}, {"amount": Decimal("541.00")}]
    fitted = _fit_to_payment(rows, Decimal("3300.00"))
    assert sum(fitted) == Decimal("3300.00")
    assert fitted[1] > fitted[0] > 0


def test_bill_date_from_purpose(db):
    line = BankStatementLine(operation_date=dt.date(2026, 2, 13), payment_purpose="По счету №7625/52505 от 31.01.26 В том числе НДС")
    assert bill_date(line) == dt.date(2026, 1, 31)
    line = BankStatementLine(operation_date=dt.date(2026, 6, 9), payment_purpose="по счету № 211301941 от 2026-06-09")
    assert bill_date(line) == dt.date(2026, 6, 9)
    line = BankStatementLine(operation_date=dt.date(2026, 3, 13), payment_purpose="Единый налоговый платеж.")
    assert bill_date(line) == dt.date(2026, 3, 13)


# --- Доступ и страница ------------------------------------------------------

def test_view_only_for_chairman(app, db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")
    resp = client.get("/year-report/")
    assert resp.status_code == 302


def test_view_renders_for_chairman(app, db, client):
    _chairman(db, client)
    resp = client.get("/year-report/?year=2026")
    assert resp.status_code == 200
    assert "Финансы за 2026 год" in resp.get_data(as_text=True)


def test_budget_import_replaces_year(app, db, client):
    _chairman(db, client)
    db.add(BudgetItem(year=2026, section="Старое", name="Старая статья", amount=Decimal("1")))
    db.commit()
    resp = client.post("/year-report/budget/import", data={
        "year": "2026", "file": (io.BytesIO(BUDGET_CSV.encode("utf-8")), "smeta.csv"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    items = db.query(BudgetItem).filter(BudgetItem.year == 2026).all()
    assert len(items) == 3
    assert sum(i.amount for i in items) == Decimal("74000.00")
    body = client.get("/year-report/?year=2026").get_data(as_text=True)
    assert "74000,00" in body


# --- Списания без расхода ---------------------------------------------------

def test_book_statement_line_creates_expense_and_linked_payment(app, db, client):
    _chairman(db, client)
    account = _bank(db)
    tns = Counterparty(name="ТНС Энерго", inn="7606052264")
    db.add(tns)
    line = _debit(db, account, dt.date(2026, 2, 13), "4103.50", 'ПАО "ТНС энерго Ярославль"', "7606052264",
                  "По счету №76250303289/52505 от 31.01.26", "u1")
    account.balance = Decimal("1000")
    db.commit()

    body = client.get("/year-report/?year=2026").get_data(as_text=True)
    assert "Списания из выписки без расхода" in body and "4103,50" in body

    resp = client.post(f"/year-report/lines/{line.id}/book", data={
        "counterparty_id": str(tns.id), "category": "Электроэнергия", "description": "Электроэнергия за январь",
    })
    assert resp.status_code == 302
    expense = db.query(Expense).one()
    assert expense.counterparty_id == tns.id and expense.date == dt.date(2026, 1, 31)
    assert expense.category == "Электроэнергия" and expense.amount == Decimal("4103.50")
    payment = db.query(CounterpartyPayment).one()
    assert payment.bank_statement_line_id == line.id and payment.adjusts_bank_balance is False
    # Баланс счёта не меняется — деньги уже списаны.
    assert db.get(BankAccount, account.id).balance == Decimal("1000")


def test_book_all_skips_unconfident_and_own_transfers(app, db, client):
    _chairman(db, client)
    account = _bank(db)
    _debit(db, account, dt.date(2026, 3, 13), "58000", "Казначейство России (ФНС России)", "7727406020",
           "Единый налоговый платеж. НДС не облагается.", "u1")
    _debit(db, account, dt.date(2026, 11, 16), "5465", 'ООО "ИНТЕРНЕТ РЕШЕНИЯ"', "7704217370",
           "Оплата по заказу 0161382282-0010", "u2")
    # Перевод на свой же счёт — не трата, в списке не показывается.
    _debit(db, account, dt.date(2026, 4, 1), "10000", "ГМ Восход", "7610037501", "Перевод собственных средств", "u3",
           counterparty_account="40703810777030002079")
    db.commit()

    resp = client.post("/year-report/lines/book-all", data={"year": "2026"})
    assert resp.status_code == 302
    expenses = db.query(Expense).all()
    assert [(e.amount, e.category) for e in expenses] == [(Decimal("58000.00"), "Земельный налог")]
    # Новый контрагент заведён по названию из выписки.
    assert expenses[0].counterparty.name == "Казначейство России (ФНС России)"


def test_mark_not_expense_hides_line(app, db, client):
    _chairman(db, client)
    account = _bank(db)
    line = _debit(db, account, dt.date(2026, 5, 1), "300", "Иванов И.И.", None, "Возврат переплаты", "u1")
    db.commit()
    client.post(f"/year-report/lines/{line.id}/not-expense")
    assert db.get(BankStatementLine, line.id).not_expense is True
    assert "Возврат переплаты" not in client.get("/year-report/?year=2026").get_data(as_text=True)


# --- Импорт отчёта по тратам ------------------------------------------------

def test_spending_import_matches_statement_orders_and_cash(app, db, client):
    _chairman(db, client)
    account = _bank(db)
    tns = Counterparty(name="ТНС Энерго", inn="7606052264")
    db.add(tns)
    _debit(db, account, dt.date(2026, 2, 13), "4103.50", 'ПАО "ТНС энерго Ярославль"', "7606052264",
           "По счету от 31.01.26", "u1")
    # Заказ на маркетплейсе: оплачено со скидкой — меньше суммы позиций.
    order_line = _debit(db, account, dt.date(2026, 7, 31), "11000", 'ООО "ИНТЕРНЕТ РЕШЕНИЯ"', "7704217370",
                        "Оплата по заказу 0161382282-0005", "u2")
    db.commit()

    resp = client.post("/year-report/spending/import", data={
        "year": "2026", "file": (io.BytesIO(SPENDING_CSV.encode("utf-8")), "report.csv"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "разница" in body  # заказ сопоставлен по номеру, сумма не совпала

    rows = parse_spending_csv(SPENDING_CSV)
    form = {"year": "2026", "csv_text": SPENDING_CSV}
    for r in rows:
        form[f"category_{r['index']}"] = r["category"]
        form[f"action_{r['index']}"] = {
            "Электроэнергия за янв 2026": "line:1",
            "Wi-Fi мост": f"line:{order_line.id}",
            "Кабель витая пара": f"line:{order_line.id}",
        }.get(r["name"], "cash")
    resp = client.post("/year-report/spending/import/apply", data=form)
    assert resp.status_code == 302

    expenses = db.query(Expense).order_by(Expense.id).all()
    assert sum(e.amount for e in expenses) == Decimal("342.00") + Decimal("4103.50") + Decimal("11000") + Decimal("5000")
    order_expenses = [e for e in expenses if e.counterparty.inn == "7704217370"]
    assert len(order_expenses) == 2 and sum(e.amount for e in order_expenses) == Decimal("11000")
    assert any("по отчёту" in e.description for e in order_expenses)
    electricity = next(e for e in expenses if e.counterparty_id == tns.id)
    assert electricity.date == dt.date(2026, 1, 31)
    cash = [e for e in expenses if e.counterparty.name == CASH_COUNTERPARTY_NAME]
    assert {e.amount for e in cash} == {Decimal("342.00"), Decimal("5000.00")}
    # Обе строки выписки привязаны к платежам.
    linked = {p.bank_statement_line_id for p in db.query(CounterpartyPayment).all()}
    assert {1, order_line.id} <= linked

    # Повторный импорт того же файла: всё уже заведено — новых расходов нет.
    resp = client.post("/year-report/spending/import", data={
        "year": "2026", "file": (io.BytesIO(SPENDING_CSV.encode("utf-8")), "report.csv"),
    }, content_type="multipart/form-data")
    assert resp.get_data(as_text=True).count("selected>уже заведён") >= 3


def test_set_expense_category_returns_to_same_page(app, db, client):
    _chairman(db, client)
    cp = Counterparty(name="Подрядчик")
    db.add(cp)
    db.flush()
    expense = Expense(counterparty_id=cp.id, date=dt.date(2026, 5, 1), amount=Decimal("100"), category="Прочее")
    db.add(expense)
    db.commit()
    resp = client.post(f"/year-report/expenses/{expense.id}/category", data={"category": "Хоз. работы"},
                       headers={"Referer": "http://localhost/year-report/?year=2026"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/year-report/?year=2026")
    assert db.get(Expense, expense.id).category == "Хоз. работы"
    body = client.get("/year-report/?year=2026").get_data(as_text=True)
    assert 'data-key="spending:Хоз. работы"' in body


def test_custom_csv_format_with_skipped_column(app, db, client):
    """Формат с пропуском столбца: дата в 1-м, сумма в 2-м, 3-й не читается,
    наименование в 4-м — позиции абсолютные, пропуск не сдвигает остальные."""
    _chairman(db, client)
    resp = client.post("/year-report/format/spending", data={
        "col_name": "1", "pos_name": "4", "col_date": "1", "pos_date": "1",
        "col_amount": "1", "pos_amount": "2",
    })
    assert resp.status_code == 302
    from app.year_report import get_csv_columns
    assert get_csv_columns("spending") == ["date", "amount", "", "name"]
    text = "Дата,Сумма,Комментарий,Наименование (хоз. работы)\n20.01.2026,5000,что угодно,Уборка снега\n"
    rows = parse_spending_csv(text, get_csv_columns("spending"))
    assert [(r["name"], r["amount"], r["section"], r["category"]) for r in rows] == [
        ("Уборка снега", Decimal("5000"), "хоз. работы", "Хоз. работы"),
    ]
    template = client.get("/year-report/format/spending/template.csv").get_data(as_text=True)
    assert template.splitlines()[0].count(",") == 3


def test_csv_format_rejects_missing_required_and_duplicate_positions(app, db, client):
    _chairman(db, client)
    from app.year_report import get_csv_columns
    client.post("/year-report/format/budget", data={"col_name": "1", "pos_name": "1"})
    client.post("/year-report/format/budget", data={
        "col_name": "1", "pos_name": "1", "col_amount": "1", "pos_amount": "1",
    })
    assert get_csv_columns("budget") == ["name", "quantity", "price", "amount"]
    client.post("/year-report/format/budget", data={
        "col_name": "1", "pos_name": "2", "col_amount": "1", "pos_amount": "1",
    })
    assert get_csv_columns("budget") == ["amount", "name"]
    items = parse_budget_csv("Сумма,Наименование\n,Раздел\n100,Статья\n", get_csv_columns("budget"))
    assert [(i["section"], i["name"], i["amount"]) for i in items] == [("Раздел", "Статья", Decimal("100"))]


# --- Смета и факт -----------------------------------------------------------

def _plan_fact_fixture(db):
    from app.models import BudgetItem as BI
    items = [
        BI(year=2026, section="Содержание кооператива", name="Операционная деятельность, платежные поручения", amount=Decimal("4000"), position=0),
        BI(year=2026, section="Содержание кооператива", name="Механизированная уборка снега", amount=Decimal("15000"), position=1),
        BI(year=2026, section="Взыскание задолженности", name="Почтовые расходы", amount=Decimal("1500"), position=2),
    ]
    db.add_all(items)
    cp = Counterparty(name="Подрядчик")
    bank = Counterparty(name="ПАО Сбербанк")
    db.add_all([cp, bank])
    db.flush()

    def exp(counterparty, amount, category, description, **kw):
        e = Expense(counterparty_id=counterparty.id, date=dt.date(2026, 3, 1), amount=Decimal(amount),
                    category=category, description=description, **kw)
        db.add(e)
        return e
    snow = exp(cp, "5000", "Хоз. работы", "Механизированная уборка снега (2 × 2 500)")
    fee = exp(bank, "103", "Банковское обслуживание", "Комиссия за перевод СБП")
    post = exp(cp, "2000", "Почтовые расходы", "Аванс Почте России")
    road = exp(cp, "21000", "Хоз. работы", "Транспортные услуги по счету №317")
    land = exp(cp, "58000", "Земельный налог", "Единый налоговый платеж")
    db.commit()
    return items, {"snow": snow, "fee": fee, "post": post, "road": road, "land": land}


def test_plan_fact_suggests_items_separates_land_tax_and_off_budget(app, db):
    from app.year_report import _plan_fact
    items, e = _plan_fact_fixture(db)
    pf = _plan_fact(2026)
    by_item = {row["item"].name: row for sec in pf["sections"] for row in sec["items"]}
    assert by_item["Механизированная уборка снега"]["fact"] == Decimal("5000")
    # Комиссия банка — единственная позиция с этой статьёй в смете.
    assert by_item["Операционная деятельность, платежные поручения"]["fact"] == Decimal("103")
    post = by_item["Почтовые расходы"]
    assert post["meter"]["over_amount"] == Decimal("500") and post["meter"]["rate"] == 133.3
    assert all(x["suggested"] for row in by_item.values() for x in row["expenses"])
    assert pf["suggested_count"] == 3
    # Подсыпка дороги — вне сметы, земельный налог — отдельно.
    assert pf["off_total"] == Decimal("21000")
    assert pf["land"]["total"] == Decimal("58000")
    assert pf["plan"] == Decimal("20500") and pf["fact"] == Decimal("7103")


def test_budget_item_select_and_confirm_routes(app, db, client):
    from app.year_report import _plan_fact
    _chairman(db, client)
    items, e = _plan_fact_fixture(db)
    # Снег явно «вне сметы», подсыпку — к позиции «уборка снега» (пусть так).
    client.post(f"/year-report/expenses/{e['snow'].id}/budget", data={"budget_item": "off"})
    client.post(f"/year-report/expenses/{e['road'].id}/budget", data={"budget_item": str(items[1].id)})
    pf = _plan_fact(2026)
    assert pf["off_total"] == Decimal("5000")
    snow_row = next(r for sec in pf["sections"] for r in sec["items"] if r["item"].id == items[1].id)
    assert snow_row["fact"] == Decimal("21000") and snow_row["expenses"][0]["suggested"] is False
    # Закрепить подбор — оставшиеся автоматические привязки сохраняются.
    resp = client.post("/year-report/plan-fact/confirm", data={"year": "2026"})
    assert resp.status_code == 302
    assert db.get(Expense, e["fee"].id).budget_item_id == items[0].id
    assert db.get(Expense, e["post"].id).budget_item_id == items[2].id
    assert _plan_fact(2026)["suggested_count"] == 0
    # «auto» снимает и явную привязку, и отметку «вне сметы».
    client.post(f"/year-report/expenses/{e['snow'].id}/budget", data={"budget_item": "auto"})
    snow = db.get(Expense, e["snow"].id)
    assert snow.off_budget is False and snow.budget_item_id is None
    body = client.get("/year-report/?year=2026").get_data(as_text=True)
    assert "Смета и факт" in body and "перерасход" in body


def test_budget_reimport_keeps_expense_links(app, db, client):
    _chairman(db, client)
    client.post("/year-report/budget/import", data={
        "year": "2026", "file": (io.BytesIO(BUDGET_CSV.encode("utf-8")), "smeta.csv"),
    }, content_type="multipart/form-data")
    item = db.query(BudgetItem).filter(BudgetItem.name == "Механизированная уборка снега").one()
    cp = Counterparty(name="Подрядчик")
    db.add(cp)
    db.flush()
    expense = Expense(counterparty_id=cp.id, date=dt.date(2026, 2, 1), amount=Decimal("100"), budget_item_id=item.id)
    db.add(expense)
    db.commit()
    client.post("/year-report/budget/import", data={
        "year": "2026", "file": (io.BytesIO(BUDGET_CSV.encode("utf-8")), "smeta.csv"),
    }, content_type="multipart/form-data")
    db.expire_all()
    new_item = db.query(BudgetItem).filter(BudgetItem.name == "Механизированная уборка снега").one()
    assert db.get(Expense, expense.id).budget_item_id == new_item.id


# --- Справочник статей и цветов ----------------------------------------------

def test_parse_keywords_prefix_and_whole_word():
    from app.year_report import parse_keywords
    prefix, whole = parse_keywords('щит, "фот"')
    assert prefix.search("Щит распределительный") and not prefix.search("Диод защитный")
    assert whole.search("ФОТ за март") and not whole.search("Фотореле IEK")


def test_categories_seeded_by_migration_with_colors(app, db):
    from app.models import ExpenseCategory, FeeType
    names = [c.name for c in db.query(ExpenseCategory).order_by(ExpenseCategory.position)]
    assert names[0] == "Земельный налог" and names[-1] == "Прочее"
    assert db.query(ExpenseCategory).filter_by(name="Земельный налог").one().outside_budget is True
    assert db.query(ExpenseCategory).filter_by(is_default=True).one().name == "Прочее"


def test_category_crud_renames_and_moves_expenses(app, db, client):
    from app.models import ExpenseCategory
    _chairman(db, client)
    cp = Counterparty(name="Подрядчик")
    db.add(cp)
    db.flush()
    db.add(Expense(counterparty_id=cp.id, date=dt.date(2026, 5, 1), amount=Decimal("100"), category="Хоз. работы"))
    db.commit()
    assert client.get("/year-report/categories").status_code == 200

    # Новая статья со своими словами — сразу участвует в автоподборе.
    client.post("/year-report/categories/new", data={
        "name": "Дорога", "color": "#123456", "keywords": "подсыпк, крошк, асфальт", "position": "5",
    })
    road = db.query(ExpenseCategory).filter_by(name="Дорога").one()
    assert road.color == "#123456"
    with app.test_request_context():
        from app.year_report import guess_category
        assert guess_category("Подсыпка дороги асфальтовой крошкой") == "Дорога"

    # Переименование статьи переименовывает и расходы.
    hoz = db.query(ExpenseCategory).filter_by(name="Хоз. работы").one()
    client.post(f"/year-report/categories/{hoz.id}/edit", data={
        "name": "Хозяйственные работы", "color": hoz.color, "keywords": hoz.keywords, "position": str(hoz.position),
    })
    db.expire_all()
    assert db.query(Expense).one().category == "Хозяйственные работы"

    # Удаление — расходы в «Прочее»; «Прочее» удалить нельзя.
    client.post(f"/year-report/categories/{hoz.id}/delete")
    db.expire_all()
    assert db.query(Expense).one().category == "Прочее"
    other = db.query(ExpenseCategory).filter_by(is_default=True).one()
    client.post(f"/year-report/categories/{other.id}/delete")
    assert db.get(ExpenseCategory, other.id) is not None


def test_chart_colors_fixed_per_category_and_fee_type(app, db, client):
    from app.models import FeeType
    from app.year_report import _chart_slices
    _chairman(db, client)
    with app.test_request_context():
        slices = _chart_slices([
            {"name": "Электрика", "amount": Decimal("10")},
            {"name": "Земельный налог", "amount": Decimal("50")},
        ], "amount")
    assert [(s["label"], s["color"]) for s in slices] == [("Земельный налог", "#2a78d6"), ("Электрика", "#eb6834")]
    fee = FeeType(code="membership_x", name="Членский взнос X")
    db.add(fee)
    db.commit()
    client.post(f"/year-report/fee-types/{fee.id}/color", data={"color": "#ABCDEF"})
    assert db.get(FeeType, fee.id).color == "#abcdef"


# --- Собрано на смету по разделам ---------------------------------------------

def test_section_funding_pro_rata_and_kinds(app, db, client):
    from app.models import BudgetSection, Charge, ChargeAllocation, FeeType, MemberAccount, Payment
    from app.year_report import _budget, _plan_fact, _with_funding, guess_section_target
    from tests.conftest import make_garage, make_person
    _chairman(db, client)
    assert guess_section_target("Замена общей проводки") and guess_section_target("Установка новых опор освещения")
    assert not guess_section_target("Содержание кооператива") and not guess_section_target("Прочее")

    db.add_all([
        BudgetItem(year=2026, section="Содержание кооператива", name="Уборка снега", amount=Decimal("30000"), position=0),
        BudgetItem(year=2026, section="Замена общей проводки", name="Провод", amount=Decimal("10000"), position=1),
    ])
    membership = db.query(FeeType).filter_by(code="membership").one_or_none() or FeeType(code="membership", name="Членский взнос")
    land = db.query(FeeType).filter_by(code="land_tax").one_or_none() or FeeType(code="land_tax", name="Земельный налог")
    db.add_all([membership, land])
    person, garage = make_person(db), make_garage(db)
    acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type=membership, account_number="10010")
    land_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type=land, account_number="20010")
    db.add_all([acc, land_acc])
    db.flush()
    charge = Charge(account_id=acc.id, year=2026, amount=Decimal("8000"))
    land_charge = Charge(account_id=land_acc.id, year=2026, amount=Decimal("1000"))
    payment = Payment(account_id=acc.id, date=dt.date(2027, 1, 10), amount=Decimal("4000"))  # заплатил в следующем году
    db.add_all([charge, land_charge, payment])
    db.flush()
    db.add(ChargeAllocation(charge_id=charge.id, payment_id=payment.id, amount=Decimal("4000")))
    cp = Counterparty(name="Русский Свет")
    db.add(cp)
    db.flush()
    db.add(Expense(counterparty_id=cp.id, date=dt.date(2026, 5, 1), amount=Decimal("1500"),
                   category="Электрика", description="Провод ПВ3"))
    db.commit()

    with app.test_request_context():
        budget = _budget(2026)
        fund = budget["funding"]
        # Земельный налог — транзит, в средства сметы не входит.
        assert (fund["charged"], fund["paid"]) == (Decimal("8000"), Decimal("4000"))
        wiring = next(s for s in budget["sections"] if s["name"] == "Замена общей проводки")
        assert wiring["is_target"] and not wiring["kind_saved"]
        assert wiring["collected"] == Decimal("1000.00")  # 25 % сметы
        assert fund["target_collected"] == Decimal("1000.00") and fund["membership_collected"] == Decimal("3000.00")
        pf = _with_funding(_plan_fact(2026), budget)
        sec = next(s for s in pf["sections"] if s["name"] == "Замена общей проводки")
        assert sec["balance"] == Decimal("-500.00")  # собрано 1000, потрачено 1500 — дефицит

    # Председатель меняет тип раздела — сохраняется и переживает перезагрузку сметы.
    client.post("/year-report/budget/section-kind", data={"year": "2026", "section": "Замена общей проводки", "kind": "membership"})
    assert db.query(BudgetSection).filter_by(year=2026, name="Замена общей проводки").one().is_target is False
    body = client.get("/year-report/?year=2026").get_data(as_text=True)
    assert "Собрано на смету — по разделам" in body


def test_section_funding_split_when_target_fee_charged(app, db):
    from app.models import Charge, ChargeAllocation, FeeType, MemberAccount, Payment
    from app.year_report import _budget
    from tests.conftest import make_garage, make_person
    db.add_all([
        BudgetItem(year=2027, section="Содержание кооператива", name="Уборка снега", amount=Decimal("30000"), position=0),
        BudgetItem(year=2027, section="Замена общей проводки", name="Провод", amount=Decimal("6000"), position=1),
        BudgetItem(year=2027, section="Установка опор освещения", name="Опоры", amount=Decimal("4000"), position=2),
    ])
    membership = db.query(FeeType).filter_by(code="membership").one_or_none() or FeeType(code="membership", name="Членский взнос")
    target = db.query(FeeType).filter_by(code="target").one_or_none() or FeeType(code="target", name="Целевой взнос")
    db.add_all([membership, target])
    person, garage = make_person(db), make_garage(db)
    m_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type=membership, account_number="10010")
    t_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type=target, account_number="00010")
    db.add_all([m_acc, t_acc])
    db.flush()
    m_charge = Charge(account_id=m_acc.id, year=2027, amount=Decimal("30000"))
    t_charge = Charge(account_id=t_acc.id, year=2027, amount=Decimal("10000"))  # целевой по смете: 6000 + 4000
    t_pay = Payment(account_id=t_acc.id, date=dt.date(2027, 3, 1), amount=Decimal("5000"))
    db.add_all([m_charge, t_charge, t_pay])
    db.flush()
    db.add(ChargeAllocation(charge_id=t_charge.id, payment_id=t_pay.id, amount=Decimal("5000")))
    db.commit()

    with app.test_request_context():
        budget = _budget(2027)
    fund = budget["funding"]
    assert fund["split"] is True
    by_name = {s["name"]: s for s in budget["sections"]}
    # Целевые 5000 — только на целевые разделы, 60/40 по плану; членские не оплачены.
    assert by_name["Замена общей проводки"]["collected"] == Decimal("3000.00")
    assert by_name["Установка опор освещения"]["collected"] == Decimal("2000.00")
    assert by_name["Содержание кооператива"]["collected"] == Decimal("0.00")
    assert by_name["Содержание кооператива"]["funded_charged"] == Decimal("30000.00")
    assert fund["target_collected"] == Decimal("5000.00")


def test_mass_charge_offers_budget_totals(app, db, client):
    from app.models import BudgetSection
    from app.year_report import budget_totals_by_kind
    _chairman(db, client)
    db.add_all([
        BudgetItem(year=2027, section="Содержание кооператива", name="Уборка снега", amount=Decimal("30000"), position=0),
        BudgetItem(year=2027, section="Замена общей проводки", name="Провод", amount=Decimal("6000"), position=1),
        BudgetItem(year=2027, section="Прочее", name="Wi-Fi мост", amount=Decimal("4000"), position=2),
        BudgetSection(year=2027, name="Прочее", is_target=True),  # председатель отметил целевым
    ])
    db.commit()
    with app.test_request_context():
        assert budget_totals_by_kind()[2027] == {"membership": 30000.0, "target": 10000.0, "total": 40000.0}
    body = client.get("/finance/mass-charge").get_data(as_text=True)
    assert 'id="budgetHint"' in body and '"2027": {"membership": 30000.0' in body
