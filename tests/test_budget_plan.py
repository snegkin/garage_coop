"""
Планирование сметы на сайте (app/budget_plan.py): разделы и позиции,
позиции из прошлых смет, возмещение людям в смете следующего года.
"""
import datetime as dt
from decimal import Decimal

from app.models import BudgetItem, BudgetSection, Counterparty, Expense, ExpenseKind, RoleEnum
from tests.conftest import login, make_person, make_user


def _chairman(db, client):
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")


def _items(db, year):
    return db.query(BudgetItem).filter_by(year=year).order_by(BudgetItem.position).all()


def test_plan_page_only_for_chairman(app, db, client):
    make_user(db, "member", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member", "pass12345")
    assert client.get("/year-report/plan?year=2027").status_code in (302, 403)


def test_sections_and_items_editing(app, db, client):
    _chairman(db, client)
    referer = {"Referer": "http://localhost/year-report/plan?year=2027"}
    assert client.get("/year-report/plan?year=2027").status_code == 200

    resp = client.post("/year-report/plan/sections/new", data={"year": "2027", "name": "Ремонт дороги", "kind": "target"},
                       headers=referer)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/year-report/plan?year=2027")
    assert db.query(BudgetSection).filter_by(year=2027, name="Ремонт дороги").one().is_target

    client.post("/year-report/plan/items/new", data={"year": "2027", "section": "Ремонт дороги", "name": "Щебень",
                                                     "quantity": "3", "price": "4 500,00", "amount": ""})
    client.post("/year-report/plan/items/new", data={"year": "2027", "section": "Содержание", "name": "Покос",
                                                     "amount": "3000"})
    client.post("/year-report/plan/items/new", data={"year": "2027", "section": "Ремонт дороги", "name": "Грейдер",
                                                     "amount": "8000"})
    items = _items(db, 2027)
    # Новая позиция раздела встаёт в конец своего раздела, а не всей сметы.
    assert [(i.section, i.name, i.amount) for i in items] == [
        ("Ремонт дороги", "Щебень", Decimal("13500.00")),
        ("Ремонт дороги", "Грейдер", Decimal("8000.00")),
        ("Содержание", "Покос", Decimal("3000.00")),
    ]
    gravel, grader = items[0], items[1]

    client.post(f"/year-report/plan/items/{gravel.id}/edit", data={"name": "Щебень фр. 20-40", "quantity": "4",
                                                                  "price": "4500", "amount": "1"})
    client.post(f"/year-report/plan/items/{grader.id}/move", data={"direction": "up"})
    db.expire_all()
    assert [(i.name, i.amount) for i in _items(db, 2027)][:2] == [("Грейдер", Decimal("8000.00")),
                                                                  ("Щебень фр. 20-40", Decimal("18000.00"))]

    client.post("/year-report/plan/sections/rename", data={"year": "2027", "old": "Ремонт дороги", "name": "Дорога"})
    db.expire_all()
    assert {i.section for i in _items(db, 2027)} == {"Дорога", "Содержание"}
    assert db.query(BudgetSection).filter_by(year=2027, name="Дорога").one().is_target

    # Удаление позиции: расход, отнесённый к ней, остаётся без позиции.
    cp = Counterparty(name="Подрядчик")
    db.add(cp)
    db.flush()
    mowing = next(i for i in _items(db, 2027) if i.name == "Покос")
    expense = Expense(counterparty_id=cp.id, date=dt.date(2027, 6, 1), amount=Decimal("3000"), category="Хоз. работы",
                      kind=ExpenseKind.CHARGE, budget_item_id=mowing.id)
    db.add(expense)
    db.commit()
    client.post(f"/year-report/plan/items/{mowing.id}/delete")
    db.expire_all()
    assert db.get(Expense, expense.id).budget_item_id is None

    client.post("/year-report/plan/sections/delete", data={"year": "2027", "name": "Дорога"})
    db.expire_all()
    assert _items(db, 2027) == [] and db.query(BudgetSection).filter_by(year=2027, name="Дорога").first() is None


def test_copy_items_from_previous_budget_with_actual_amount(app, db, client):
    _chairman(db, client)
    db.add(BudgetSection(year=2026, name="Замена проводки", is_target=True))
    wire = BudgetItem(year=2026, section="Замена проводки", name="Кабель", quantity=Decimal("100"),
                      price=Decimal("300"), amount=Decimal("30000"), position=0)
    snow = BudgetItem(year=2026, section="Содержание", name="Уборка снега", amount=Decimal("15000"), position=1)
    db.add_all([wire, snow])
    db.commit()
    body = client.get("/year-report/plan?year=2027").get_data(as_text=True)
    assert "Смета 2026" in body and "Уборка снега" in body

    client.post("/year-report/plan/copy-items", data={"year": "2027", "item_id": [str(wire.id), str(snow.id)],
                                                      f"amount_{wire.id}": "30 000,00",
                                                      f"amount_{snow.id}": "20000"})
    items = {i.name: i for i in _items(db, 2027)}
    assert items["Кабель"].amount == Decimal("30000") and items["Кабель"].quantity == Decimal("100")
    assert items["Уборка снега"].amount == Decimal("20000") and items["Уборка снега"].price is None
    assert db.query(BudgetSection).filter_by(year=2027, name="Замена проводки").one().is_target
    # Повторно та же позиция не добавляется.
    client.post("/year-report/plan/copy-items", data={"year": "2027", "item_id": [str(snow.id)]})
    assert len(_items(db, 2027)) == 2


def test_reimbursement_planned_and_paid_in_plan_fact(app, db, client):
    """Человек потратил за свой счёт в 2026, кооперативу не из чего вернуть —
    возмещение закладывается в смету 2027; выплата в 2027 — факт позиции."""
    from app.accounting import pay_counterparty
    from app.year_report import _person_counterparty, _plan_fact
    _chairman(db, client)
    person = make_person(db, full_name="Егоров Станислав Николаевич")
    with app.test_request_context():
        cp = _person_counterparty(person)
    db.add(Expense(counterparty_id=cp.id, date=dt.date(2026, 7, 25), amount=Decimal("905"), category="Прочее",
                   description="Детали — за свой счёт, к возмещению", kind=ExpenseKind.CHARGE, person_id=person.id))
    db.commit()

    body = client.get("/year-report/plan?year=2027").get_data(as_text=True)
    assert "Егоров Станислав Николаевич" in body and "Заложить в смету" in body
    # Долг этого же года в смету этого же года не предлагается (трата уже в факте года).
    assert "Заложить в смету" not in client.get("/year-report/plan?year=2026").get_data(as_text=True)

    client.post("/year-report/plan/reimburse", data={"year": "2027", "person_id": str(person.id), "amount": "905"})
    item = db.query(BudgetItem).filter_by(year=2027).one()
    assert item.person_id == person.id and item.amount == Decimal("905")
    assert item.section == "Возмещение затрат людям"
    assert db.query(BudgetSection).filter_by(year=2027, name="Возмещение затрат людям").one().is_target is False

    pay_counterparty(cp, dt.date(2027, 3, 1), Decimal("905"), comment="Возмещение", adjust_balance=False)
    db.commit()
    with app.test_request_context("/year-report/?year=2027"):
        pf = _plan_fact(2027)
    row = pf["sections"][0]["items"][0]
    assert row["fact"] == Decimal("905") and pf["fact"] == Decimal("905")


def test_item_from_off_budget_expenses(app, db, client):
    """Траты, которых не было в смете, — новой позицией с планом 0 (в
    существующем или новом разделе); в «Смете и факт» они уже не вне сметы."""
    from app.year_report import _plan_fact
    _chairman(db, client)
    db.add(BudgetItem(year=2026, section="Содержание", name="Покос", amount=Decimal("3000"), position=0))
    cp = Counterparty(name="Ubiquiti")
    db.add(cp)
    db.flush()
    cams = [Expense(counterparty_id=cp.id, date=dt.date(2026, 7, d), amount=Decimal(a), category="Видеонаблюдение",
                    kind=ExpenseKind.CHARGE, off_budget=True) for d, a in ((1, "13691"), (4, "2400"))]
    db.add_all(cams)
    db.commit()
    body = client.get("/year-report/?year=2026").get_data(as_text=True)
    assert "сделать позицией сметы" in body and "новая позиция (не было в смете)" in body

    resp = client.post("/year-report/plan/items/from-expenses", data={
        "year": "2026", "section": "", "new_section": "Видеонаблюдение", "kind": "target",
        "name": "Wi-Fi мост и камеры", "expense_ids": ",".join(str(e.id) for e in cams)},
        headers={"Referer": "http://localhost/year-report/?year=2026"})
    assert resp.status_code == 302 and "/year-report/?year=2026" in resp.headers["Location"]
    item = db.query(BudgetItem).filter_by(year=2026, name="Wi-Fi мост и камеры").one()
    assert item.amount == 0 and item.section == "Видеонаблюдение"
    assert db.query(BudgetSection).filter_by(year=2026, name="Видеонаблюдение").one().is_target
    db.expire_all()
    assert all(db.get(Expense, e.id).budget_item_id == item.id and not db.get(Expense, e.id).off_budget for e in cams)
    with app.test_request_context("/year-report/?year=2026"):
        pf = _plan_fact(2026)
    assert pf["off_total"] == 0
    row = next(r for s in pf["sections"] for r in s["items"] if r["item"].id == item.id)
    assert row["plan"] == 0 and row["fact"] == Decimal("16091")
    assert "не было в смете" in client.get("/year-report/?year=2026").get_data(as_text=True)
    # В планировании следующего года — «из прошлых смет» с фактом как суммой по умолчанию.
    body = client.get("/year-report/plan?year=2027").get_data(as_text=True)
    assert "Wi-Fi мост и камеры" in body and "16091,00" in body
