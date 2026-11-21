"""
Планирование сметы на сайте («Финансы за год» → «Планирование сметы»).

Смета года — позиции BudgetItem по разделам (BudgetSection — членский или
целевой). Здесь её можно составить и поправить без CSV:
- разделы: добавить, переименовать, членский/целевой, удалить;
- позиции: добавить, изменить (кол-во × цена или сумма), удалить, сдвинуть;
- взять позиции из смет прошлых лет — рядом их план и факт (сколько
  реально потрачено), чтобы заложить реальную сумму;
- заложить возмещение людям: долг кооператива человеку за траты прошлых
  лет за свой счёт или начисленную оплату труда, которую не на что было
  выплатить, — позицией «Возмещение затрат: ФИО» (BudgetItem.person_id).
  Факт по ней — выплаты этому человеку за год (year_report._plan_fact).

Маршруты на том же blueprint, что и отчёт (url_prefix /year-report), только
председателю. Действия — AJAX-формы (data-ajax), ответ — та же страница.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for

from . import audit, database
from .accounting import cooperative_balance
from .auth import roles_required
from .i18n import translate as _
from .models import BankAccount, BudgetItem, BudgetSection, Expense, FeeType, MemberAccount, Person, RoleEnum
from .year_report import (
    _plan_fact, _redirect_back, bp, cap, guess_section_target, is_target_fee, owed_people, parse_money,
)

REIMBURSE_SECTION = "Возмещение затрат людям"
PREVIOUS_YEARS_SHOWN = 3


# --- Данные страницы --------------------------------------------------------

def year_sections(year: int) -> list[dict]:
    """Разделы сметы года по порядку позиций, с позициями; разделы, у
    которых позиций пока нет (только что добавленные), — в конце."""
    items = (database.db_session.query(BudgetItem).filter(BudgetItem.year == year)
             .order_by(BudgetItem.position, BudgetItem.id).all())
    saved = {row.name: row.is_target
             for row in database.db_session.query(BudgetSection).filter(BudgetSection.year == year)}
    sections: dict[str, dict] = {}
    for item in items:
        sections.setdefault(item.section, {"name": item.section, "items": []})["items"].append(item)
    for name in saved:
        sections.setdefault(name, {"name": name, "items": []})
    result = list(sections.values())
    for sec in result:
        sec["kind_saved"] = sec["name"] in saved
        sec["is_target"] = saved[sec["name"]] if sec["name"] in saved else guess_section_target(sec["name"])
        sec["amount"] = sum((i.amount for i in sec["items"]), Decimal("0"))
    return result


def _accounts_count(target: bool) -> int:
    """Сколько открытых лицевых счетов членского (target=False) или
    целевого взноса — на сколько гаражей делится смета."""
    accounts = (database.db_session.query(MemberAccount).join(FeeType)
                .filter(MemberAccount.is_archived.is_(False)).all())
    return sum(1 for a in accounts
               if (is_target_fee(a.fee_type) if target else a.fee_type.code == "membership"))


def _previous_budgets(year: int) -> list[dict]:
    """Сметы прошлых лет (до PREVIOUS_YEARS_SHOWN, новые сначала) — с
    фактом по каждой позиции; уже взятые в смету года отмечены."""
    have = {(i.section, i.name) for i in database.db_session.query(BudgetItem).filter(BudgetItem.year == year)}
    years = [y for (y,) in database.db_session.query(BudgetItem.year).filter(BudgetItem.year < year)
             .distinct().order_by(BudgetItem.year.desc()).limit(PREVIOUS_YEARS_SHOWN)]
    result = []
    for prev in years:
        plan_fact = _plan_fact(prev)
        sections = []
        for sec in plan_fact["sections"]:
            rows = [{"item": r["item"], "plan": r["plan"], "fact": r["fact"],
                     "taken": (r["item"].section, r["item"].name) in have}
                    for r in sec["items"] if r["item"].person_id is None]
            if rows:
                sections.append({"name": sec["name"], "plan": sec["plan"], "fact": sec["fact"], "rows": rows})
        result.append({"year": prev, "sections": sections, "plan": plan_fact["plan"], "fact": plan_fact["fact"]})
    return result


def _reimbursements(year: int) -> list[dict]:
    """Кому кооператив должен за прошлые годы (траты за свой счёт к
    возмещению, начисленная оплата труда) и сколько уже заложено в смету
    года. Долги этого же года не предлагаем: сама трата уже в факте года —
    возмещение в той же смете посчиталось бы дважды."""
    planned: dict[int, Decimal] = {}
    for item in database.db_session.query(BudgetItem).filter(BudgetItem.year == year,
                                                             BudgetItem.person_id.isnot(None)):
        planned[item.person_id] = planned.get(item.person_id, Decimal("0")) + item.amount
    rows = []
    for row in owed_people():
        person = row["person"]
        if person is None:
            continue
        unpaid = [u for u in row["unpaid"] if u["expense"].date.year < year]
        past = sum((u["left"] for u in unpaid), Decimal("0"))
        already = planned.pop(person.id, Decimal("0"))
        if past > 0 or already > 0:
            rows.append({"person": person, "counterparty": row["counterparty"], "owed": past,
                         "planned": already, "to_plan": max(past - already, Decimal("0")), "unpaid": unpaid})
    for person_id, already in planned.items():  # заложено, а долга уже нет (выплатили)
        person = database.db_session.get(Person, person_id)
        if person is not None:
            rows.append({"person": person, "counterparty": None, "owed": Decimal("0"), "planned": already,
                         "to_plan": Decimal("0"), "unpaid": []})
    return rows


@bp.route("/plan")
@roles_required(RoleEnum.CHAIRMAN)
def plan_view():
    today = dt.date.today()
    year = request.args.get("year", type=int) or today.year + 1
    budget_years = {y for (y,) in database.db_session.query(BudgetItem.year).distinct()}
    years = sorted(budget_years | {today.year, today.year + 1, year})
    sections = year_sections(year)
    membership = sum((s["amount"] for s in sections if not s["is_target"]), Decimal("0"))
    target = sum((s["amount"] for s in sections if s["is_target"]), Decimal("0"))
    membership_accounts, target_accounts = _accounts_count(False), _accounts_count(True)
    balance_dates = [d for (d,) in database.db_session.query(BankAccount.balance_updated_at)
                     .filter(BankAccount.is_archived.is_(False)) if d]
    return render_template(
        "year_report/plan.html",
        year=year, years=years, sections=sections,
        total=membership + target, membership=membership, target=target,
        membership_accounts=membership_accounts, target_accounts=target_accounts,
        per_garage_membership=(membership / membership_accounts).quantize(Decimal("0.01")) if membership_accounts else None,
        per_garage_target=(target / target_accounts).quantize(Decimal("0.01")) if target_accounts else None,
        previous=_previous_budgets(year),
        reimbursements=_reimbursements(year),
        bank_balance=cooperative_balance(), bank_balance_date=max(balance_dates) if balance_dates else None,
        reimburse_section=REIMBURSE_SECTION,
    )


# --- Позиции ----------------------------------------------------------------

def _plan_redirect(year: int):
    return _redirect_back(year) if request.referrer else redirect(url_for("year_report.plan_view", year=year))


def _amounts(form) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Кол-во, цена, сумма из формы позиции: есть кол-во и цена — сумма =
    кол-во × цена, иначе — как введена."""
    try:
        quantity = Decimal((form.get("quantity") or "").replace(" ", "").replace(",", ".")) \
            if (form.get("quantity") or "").strip() else None
    except InvalidOperation:
        quantity = None
    price = parse_money(form.get("price"))
    amount = parse_money(form.get("amount"))
    if quantity is not None and price is not None:
        amount = (quantity * price).quantize(Decimal("0.01"))
    if quantity is not None and (quantity <= 0 or quantity >= Decimal("1e10")):
        quantity = None
    return quantity, price, amount


def _renumber(year: int, ordered: list[BudgetItem]) -> None:
    for position, item in enumerate(ordered):
        item.position = position


def _ordered_items(year: int) -> list[BudgetItem]:
    """Позиции года по разделам (в порядке year_sections) — новая позиция
    раздела встаёт в конец своего раздела, а не всей сметы."""
    return [item for sec in year_sections(year) for item in sec["items"]]


def _ensure_section(year: int, name: str, is_target: bool | None = None) -> None:
    row = database.db_session.query(BudgetSection).filter_by(year=year, name=name).first()
    if row is None:
        database.db_session.add(BudgetSection(
            year=year, name=name, is_target=guess_section_target(name) if is_target is None else is_target))
    elif is_target is not None:
        row.is_target = is_target


def _section_name(raw: str | None) -> str:
    return cap(" ".join((raw or "").split()), BudgetSection.__table__.c.name) or ""


def _insert_item(year: int, section: str, item: BudgetItem) -> None:
    ordered = _ordered_items(year)
    last = max((i for i, it in enumerate(ordered) if it.section == section), default=None)
    ordered.insert(len(ordered) if last is None else last + 1, item)
    database.db_session.add(item)
    _renumber(year, ordered)


@bp.route("/plan/items/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_add_item():
    f = request.form
    year = f.get("year", type=int)
    section = _section_name(f.get("section"))
    name = " ".join((f.get("name") or "").split())
    if year is None or not section:
        abort(400)
    quantity, price, amount = _amounts(f)
    if not name or amount is None or amount < 0:
        flash(_("Укажите наименование и сумму (или кол-во и цену)."), "danger")
        return _plan_redirect(year)
    _ensure_section(year, section)
    _insert_item(year, section, BudgetItem(year=year, section=section, name=name, quantity=quantity,
                                           price=price, amount=amount))
    database.db_session.commit()
    return _plan_redirect(year)


@bp.route("/plan/items/<int:item_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_edit_item(item_id):
    item = database.db_session.get(BudgetItem, item_id)
    if item is None:
        abort(404)
    f = request.form
    name = " ".join((f.get("name") or "").split())
    quantity, price, amount = _amounts(f)
    if not name or amount is None or amount < 0:
        flash(_("Укажите наименование и сумму (или кол-во и цену)."), "danger")
        return _plan_redirect(item.year)
    item.name, item.quantity, item.price, item.amount = name, quantity, price, amount
    database.db_session.commit()
    return _plan_redirect(item.year)


@bp.route("/plan/items/<int:item_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_delete_item(item_id):
    item = database.db_session.get(BudgetItem, item_id)
    if item is None:
        abort(404)
    year = item.year
    # Расходы, отнесённые к позиции, остаются — просто без позиции (снова
    # подбираются автоматически).
    for expense in database.db_session.query(Expense).filter(Expense.budget_item_id == item.id):
        expense.budget_item_id = None
    database.db_session.delete(item)
    database.db_session.commit()
    return _plan_redirect(year)


@bp.route("/plan/items/<int:item_id>/move", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_move_item(item_id):
    """Сдвинуть позицию вверх/вниз внутри её раздела."""
    item = database.db_session.get(BudgetItem, item_id)
    if item is None:
        abort(404)
    ordered = _ordered_items(item.year)
    i = ordered.index(item)
    j = i - 1 if request.form.get("direction") == "up" else i + 1
    if 0 <= j < len(ordered) and ordered[j].section == item.section:
        ordered[i], ordered[j] = ordered[j], ordered[i]
        _renumber(item.year, ordered)
        database.db_session.commit()
    return _plan_redirect(item.year)


# --- Разделы ----------------------------------------------------------------

@bp.route("/plan/sections/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_add_section():
    year = request.form.get("year", type=int)
    name = _section_name(request.form.get("name"))
    if year is None:
        abort(400)
    if not name:
        flash(_("Укажите название раздела."), "danger")
        return _plan_redirect(year)
    _ensure_section(year, name, request.form.get("kind") == "target")
    database.db_session.commit()
    return _plan_redirect(year)


@bp.route("/plan/sections/rename", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_rename_section():
    year = request.form.get("year", type=int)
    old = _section_name(request.form.get("old"))
    new = _section_name(request.form.get("name"))
    if year is None or not old:
        abort(400)
    if not new or new == old:
        return _plan_redirect(year)
    if any(s["name"] == new for s in year_sections(year)):
        flash(_("Раздел «{name}» уже есть в смете.", name=new), "danger")
        return _plan_redirect(year)
    for item in database.db_session.query(BudgetItem).filter_by(year=year, section=old):
        item.section = new
    row = database.db_session.query(BudgetSection).filter_by(year=year, name=old).first()
    if row is not None:
        row.name = new
    database.db_session.commit()
    return _plan_redirect(year)


@bp.route("/plan/sections/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_delete_section():
    year = request.form.get("year", type=int)
    name = _section_name(request.form.get("name"))
    if year is None or not name:
        abort(400)
    items = database.db_session.query(BudgetItem).filter_by(year=year, section=name).all()
    if items:
        for expense in database.db_session.query(Expense).filter(
                Expense.budget_item_id.in_([i.id for i in items])):
            expense.budget_item_id = None
    total = sum((i.amount for i in items), Decimal("0"))
    for item in items:
        database.db_session.delete(item)
    database.db_session.query(BudgetSection).filter_by(year=year, name=name).delete()
    if items:
        audit.record("budget.section_delete",
                     summary=f"Из сметы {year} года удалён раздел «{name}»: {len(items)} позиций на "
                             f"{audit.format_amount(total)}")
    database.db_session.commit()
    return _plan_redirect(year)


# --- Из прошлых смет и возмещение -------------------------------------------

@bp.route("/plan/copy-items", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_copy_items():
    """Выбранные позиции смет прошлых лет — в смету года, в раздел с тем же
    названием (раздел создаётся с тем же назначением). Сумма — плановая
    или фактическая, как выбрано (amount_<id>)."""
    year = request.form.get("year", type=int)
    if year is None:
        abort(400)
    ids = [int(v) for v in request.form.getlist("item_id") if v.isdigit()]
    have = {(i.section, i.name) for i in database.db_session.query(BudgetItem).filter(BudgetItem.year == year)}
    added = 0
    for source in (database.db_session.query(BudgetItem).filter(BudgetItem.id.in_(ids), BudgetItem.year < year)
                   .order_by(BudgetItem.year.desc(), BudgetItem.position)):
        if (source.section, source.name) in have:
            continue
        amount = parse_money(request.form.get(f"amount_{source.id}"))
        if amount is None or amount < 0:
            amount = source.amount
        same = amount == source.amount
        prev_kind = database.db_session.query(BudgetSection).filter_by(year=source.year, name=source.section).first()
        _ensure_section(year, source.section, prev_kind.is_target if prev_kind else None)
        _insert_item(year, source.section, BudgetItem(
            year=year, section=source.section, name=source.name, amount=amount,
            quantity=source.quantity if same else None, price=source.price if same else None,
        ))
        database.db_session.flush()
        have.add((source.section, source.name))
        added += 1
    database.db_session.commit()
    if added:
        flash(_("В смету добавлено позиций: {count}.", count=added), "success")
    return _plan_redirect(year)


@bp.route("/plan/reimburse", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_reimburse():
    """Заложить в смету года возмещение человеку — позиция «Возмещение
    затрат: ФИО» в разделе REIMBURSE_SECTION (членский)."""
    year = request.form.get("year", type=int)
    person = database.db_session.get(Person, request.form.get("person_id", type=int) or 0)
    amount = parse_money(request.form.get("amount"))
    if year is None or person is None:
        abort(400)
    if amount is None or amount <= 0:
        flash(_("Укажите сумму возмещения."), "danger")
        return _plan_redirect(year)
    _ensure_section(year, REIMBURSE_SECTION, False)
    name = cap(f"Возмещение затрат: {person.full_name}", BudgetItem.__table__.c.name)
    existing = database.db_session.query(BudgetItem).filter_by(year=year, person_id=person.id).first()
    if existing is not None:
        existing.amount += amount
    else:
        _insert_item(year, REIMBURSE_SECTION, BudgetItem(year=year, section=REIMBURSE_SECTION, name=name,
                                                          amount=amount, person_id=person.id))
    audit.record("budget.reimburse",
                 summary=f"В смету {year} года заложено возмещение {person.full_name}: {audit.format_amount(amount)}")
    database.db_session.commit()
    flash(_("Возмещение заложено в смету {year} года.", year=year), "success")
    return _plan_redirect(year)


# --- Позиция по факту трат («не было в смете») ------------------------------

@bp.route("/plan/items/from-expenses", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def plan_item_from_expenses():
    """Позиция сметы с планом 0 для трат, которых в смете не было (из
    «Смета и факт»: выбор позиции у траты или группа «вне сметы»): раздел —
    существующий или новый, траты относятся к новой позиции. В «Смете и
    факт» видно, на что потрачено сверх сметы; в смету следующего года
    позицию можно взять по факту («Из прошлых смет»)."""
    f = request.form
    year = f.get("year", type=int)
    if year is None:
        abort(400)
    section = _section_name(f.get("section")) or _section_name(f.get("new_section"))
    name = cap(" ".join((f.get("name") or "").split()), BudgetItem.__table__.c.name)
    if not section or not name:
        flash(_("Укажите раздел и наименование позиции."), "danger")
        return _plan_redirect(year)
    new_section = not f.get("section")
    _ensure_section(year, section, (f.get("kind") == "target") if new_section else None)
    item = database.db_session.query(BudgetItem).filter_by(year=year, section=section, name=name).first()
    if item is None:
        item = BudgetItem(year=year, section=section, name=name, amount=Decimal("0"))
        _insert_item(year, section, item)
        database.db_session.flush()
    ids = [int(v) for v in (f.get("expense_ids") or "").split(",") if v.strip().isdigit()]
    expenses = database.db_session.query(Expense).filter(Expense.id.in_(ids)).all() if ids else []
    for expense in expenses:
        expense.budget_item_id, expense.off_budget = item.id, False
    database.db_session.commit()
    flash(_("Позиция «{name}» (не было в смете) — в разделе «{section}»; трат отнесено: {count}.",
            name=name, section=section, count=len(expenses)), "success")
    return _plan_redirect(year)
