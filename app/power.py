import datetime as dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, abort

from . import database
from . import audit
from .i18n import translate as _, fmt2, parse_decimal, parse_optional_decimal
from .auth import roles_required
from .models import Counterparty, ElectricityTariff, ElectricityTariffKind, MasterMeterReading, Document, DocumentType, RoleEnum, Expense, BankAccount
from .accounting import (
    get_electricity_settings, current_tariff, outside_territory_rate, member_rate_below_floor, pay_counterparty, reallocate_counterparty_expenses, expense_paid_amount,
    counterparty_balance, available_statement_lines, resolve_statement_line, lighting_watts, parse_lighting,
)
from .uploads import save_upload

bp = Blueprint("power", __name__, url_prefix="/power")


def _readings_with_amounts(readings_desc):
    """
    readings_desc — список MasterMeterReading, отсортированный по (год, месяц) по убыванию.
    Возвращает список (reading, amount, delta):
    - delta = текущие показания − предыдущие (чистая дельта счётчика, для справки);
    - amount = (delta + потери в линии) × тариф — «начисленный объём» энергосбыта,
      не чистая дельта (см. MasterMeterReading.line_loss).
    Оба None для самой первой по времени записи (не с чем сравнивать).
    """
    chronological = list(reversed(readings_desc))
    result = []
    previous = None
    for r in chronological:
        if previous is None:
            amount = None
            delta = None
        else:
            delta = r.reading - previous.reading
            billed_kwh = delta + (r.line_loss or Decimal("0"))
            amount = (billed_kwh * r.tariff.rate).quantize(Decimal("0.01")) if billed_kwh > 0 else Decimal("0.00")
        result.append((r, amount, delta))
        previous = r
    result.reverse()
    return result


def _attach_reading_document(reading, file_storage, title=None):
    """Прикрепляет загруженный файл к показанию общего счётчика: новый
    Document привязывается к показанию и (если есть) к расходу перед
    поставщиком. Прежний документ, если был, не удаляется — остаётся в
    разделе «Документы» и у расхода, показание просто ссылается на новый.
    Возвращает созданный Document или None, если файла нет / расширение
    не разрешено.

    Это счёт поставщика, поэтому — как и прочие документы по расчётам с
    контрагентом (counterparties._save_document) — он внутренний
    (is_internal: только правлению) и привязан к контрагенту: к тому, на
    кого начислен расход по показанию, а без расхода — к текущему
    поставщику из настроек."""
    file_path = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"])
    if not file_path:
        return None
    title = title or _(
        "Счёт за электроэнергию {month}.{year}", month=reading.month, year=reading.year
    )
    counterparty_id = reading.expense.counterparty_id if reading.expense else get_electricity_settings().supplier_id
    doc = Document(
        doc_type=DocumentType.INVOICE, date=dt.date.today(), title=title, file_path=file_path,
        expense_id=reading.expense_id, counterparty_id=counterparty_id, is_internal=True,
    )
    database.db_session.add(doc)
    database.db_session.flush()
    reading.document_id = doc.id
    return doc


def _tariff_pairs():
    """История тарифов парами по дате начала: тариф поставщика и тариф для
    членов кооператива всегда меняются вместе (одна форма, две ставки —
    см. add_tariff), но в БД это по-прежнему две записи ElectricityTariff
    разного kind с одной effective_date: показания общего счётчика
    ссылаются на запись поставщика (MasterMeterReading.tariff_id), а
    current_tariff ищет каждую по своему kind. Старые записи с разными
    датами у двух видов показываются отдельными строками с «—» во второй
    ставке. Каждая пара — с датой окончания (день перед следующей)."""
    tariffs = (
        database.db_session.query(ElectricityTariff)
        .order_by(ElectricityTariff.effective_date.desc(), ElectricityTariff.id.desc())
        .all()
    )
    by_date: dict = {}
    for t in tariffs:
        pair = by_date.setdefault(t.effective_date, {"date": t.effective_date, "supplier": None, "member": None})
        key = "supplier" if t.kind == ElectricityTariffKind.SUPPLIER else "member"
        if pair[key] is None:
            pair[key] = t
    pairs = sorted(by_date.values(), key=lambda p: p["date"], reverse=True)
    for i, pair in enumerate(pairs):
        pair["end_date"] = pairs[i - 1]["date"] - dt.timedelta(days=1) if i > 0 else None
        pair["comment"] = (pair["supplier"] and pair["supplier"].comment) or (pair["member"] and pair["member"].comment)
        pair["member_floor"] = member_rate_below_floor(
            pair["supplier"] and pair["supplier"].rate, pair["member"] and pair["member"].rate
        )
    return pairs


def _flash_member_rate_floor(supplier_rate, member_rate) -> None:
    """Предупреждение (не ошибка — сохранить можно): тариф для членов ниже
    тарифа поставщика с потерями в сетях (accounting.member_rate_floor)."""
    floor = member_rate_below_floor(supplier_rate, member_rate)
    if floor is not None:
        flash(_(
            "Тариф для членов кооператива ({member} ₽) ниже тарифа поставщика с потерями в сетях ({floor} ₽): "
            "потери, которые поставщик выставляет сверх показаний, кооператив будет оплачивать из взносов."
        ).format(member=fmt2(member_rate), floor=fmt2(floor)), "warning")


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    settings = get_electricity_settings()
    tariff_pairs = _tariff_pairs()
    supplier_tariff = current_tariff(ElectricityTariffKind.SUPPLIER)
    member_tariff = current_tariff(ElectricityTariffKind.MEMBER)
    # Действующая сейчас пара — последняя по дате, не позже сегодня.
    current_pair = next((p for p in tariff_pairs if p["date"] <= dt.date.today()), None)

    readings = (
        database.db_session.query(MasterMeterReading)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .all()
    )
    readings_with_amounts = _readings_with_amounts(readings)
    expense_paid = {
        r.id: expense_paid_amount(r.expense) for r, _amt, _delta in readings_with_amounts if r.expense_id
    }
    bank_accounts = database.db_session.query(BankAccount).order_by(BankAccount.is_primary.desc(), BankAccount.bank_name).all()
    counterparties = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    supplier_balance = counterparty_balance(settings.supplier) if settings.supplier else None
    # По умолчанию форма внесения показаний предлагает предыдущий календарный
    # месяц от сегодняшней даты — показания вносятся постфактум (в начале
    # месяца — за предыдущий), поэтому это обычно и есть следующий по счёту
    # месяц после уже внесённых показаний.
    today = dt.date.today()
    default_reading_month_date = dt.date(today.year, today.month, 1) - dt.timedelta(days=1)
    return render_template(
        "power/view.html",
        settings=settings,
        supplier_tariff=supplier_tariff,
        member_tariff=member_tariff,
        outside_rate=outside_territory_rate(member_tariff.rate) if member_tariff else None,
        tariff_pairs=tariff_pairs,
        current_pair=current_pair,
        readings=readings_with_amounts,
        expense_paid=expense_paid,
        default_reading_year=default_reading_month_date.year,
        default_reading_month=default_reading_month_date.month,
        bank_accounts=bank_accounts,
        counterparties=counterparties,
        supplier_balance=supplier_balance,
        referenceable_statement_lines=available_statement_lines(),
        lighting_w=lighting_watts(settings.outdoor_lighting),
        today=today,
    )


@bp.route("/supplier", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def save_supplier():
    """
    Поставщик электроэнергии — не отдельная сущность, а ссылка на запись в
    общем справочнике контрагентов (раздел «Контрагенты»): здесь только
    выбор из списка. Реквизиты (ИНН, телефон и т.д.) правятся в карточке
    контрагента — там же видна вся история расходов/платежей по нему.
    Баланс расчётов с поставщиком (accounting.counterparty_balance) — это
    и есть та сумма, на которую зачисляются оплаты за электроэнергию:
    отдельного «баланса поставщика» не существует.
    """
    settings = get_electricity_settings()
    counterparty_id = request.form.get("counterparty_id")
    settings.supplier_id = int(counterparty_id) if counterparty_id else None
    audit.record(
        "power.supplier_set",
        f"Поставщик электроэнергии изменён на: {settings.supplier.name if settings.supplier else '—'}",
    )
    database.db_session.commit()
    flash(_("Данные поставщика сохранены."), "success")
    return redirect(url_for("power.view"))


@bp.route("/outside-line-loss", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def save_outside_line_loss():
    """Потери в линии до кооператива, % — надбавка к тарифу для членов у
    гаражей за территорией (см. accounting.garage_electricity_rate), и
    самопотребление их счётчиков, Вт (accounting.meter_self_consumption_kwh).
    Уже внесённые показания не пересчитываются — ставка и кВт·ч в них
    сохранены."""
    try:
        percent = parse_decimal(request.form.get("outside_line_loss_percent", ""))
    except (InvalidOperation, ValueError):
        percent = None
    if percent is None or not (Decimal("0") <= percent < Decimal("100")):
        flash(_("Укажите процент потерь от 0 до 100."), "danger")
        return redirect(url_for("power.view"))
    # Самопотребление счётчика, Вт (accounting.meter_self_consumption_kwh);
    # поля нет в форме — не меняется.
    watts = None
    if request.form.get("meter_self_consumption_w") is not None:
        try:
            watts = parse_decimal(request.form.get("meter_self_consumption_w", ""))
        except (InvalidOperation, ValueError):
            watts = None
        if watts is None or not (Decimal("0") <= watts <= Decimal("50")):
            flash(_("Укажите самопотребление счётчика от 0 до 50 Вт."), "danger")
            return redirect(url_for("power.view"))
    settings = get_electricity_settings()
    settings.outside_line_loss_percent = percent
    summary = f"Потери в линии для гаражей за территорией: {percent}%"
    if watts is not None:
        settings.meter_self_consumption_w = watts
        summary += f", самопотребление счётчика: {watts} Вт"
    audit.record("power.outside_line_loss_set", summary)
    database.db_session.commit()
    flash(_("Процент потерь сохранён."), "success")
    # Процент — тот же, что в минимуме тарифа для членов: проверить
    # последний тариф (действующий или будущий) с новым процентом.
    latest = next(iter(_tariff_pairs()), None)
    if latest and latest["supplier"] and latest["member"]:
        _flash_member_rate_floor(latest["supplier"].rate, latest["member"].rate)
    return redirect(url_for("power.view"))


@bp.route("/outdoor-lighting", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def save_outdoor_lighting():
    """Наружное освещение (состав светильников «3×50, 5×8») и широта — для
    оценки в «Финансах за год», сколько из счёта энергосбыта уходит на
    фонари (accounting.lighting_watts × accounting.night_hours)."""
    text = " ".join((request.form.get("outdoor_lighting") or "").split())
    try:
        parts = parse_lighting(text)
    except ValueError as exc:
        flash(_("Не разобрать «{part}»: пишите «кол-во×Вт» через запятую, например «3×50, 5×8».", part=str(exc)), "danger")
        return redirect(url_for("power.view"))
    try:
        latitude = parse_decimal(request.form.get("latitude", ""))
    except (InvalidOperation, ValueError):
        latitude = None
    if latitude is None or not (Decimal("-66") <= latitude <= Decimal("66")):
        flash(_("Укажите широту в градусах, от −66 до 66 (Ярославль — 57,6)."), "danger")
        return redirect(url_for("power.view"))
    settings = get_electricity_settings()
    settings.outdoor_lighting = ", ".join(f"{c}×{w.normalize():f}" for c, w in parts)[:200] or None
    settings.latitude = latitude
    database.db_session.commit()
    flash(_("Наружное освещение сохранено."), "success")
    return redirect(url_for("power.view"))


def _parse_tariff_form(f):
    """Дата и обе ставки из формы тарифа; ошибка — текстом."""
    try:
        effective_date = dt.date.fromisoformat(f.get("effective_date", ""))
        supplier_rate = parse_decimal(f.get("supplier_rate", ""))
        member_rate = parse_decimal(f.get("member_rate", ""))
    except (ValueError, InvalidOperation):
        return None, _("Укажите дату и обе ставки — поставщика и для членов кооператива.")
    if supplier_rate is None or member_rate is None or supplier_rate <= 0 or member_rate <= 0:
        return None, _("Укажите дату и обе ставки — поставщика и для членов кооператива.")
    return (effective_date, supplier_rate, member_rate, f.get("comment") or None), None


def _pair_on(date: dt.date) -> dict:
    rows = database.db_session.query(ElectricityTariff).filter_by(effective_date=date).all()
    return {
        "supplier": next((t for t in rows if t.kind == ElectricityTariffKind.SUPPLIER), None),
        "member": next((t for t in rows if t.kind == ElectricityTariffKind.MEMBER), None),
    }


def _supplier_tariff_used(tariff) -> bool:
    """На тариф поставщика ссылается показание общего счётчика
    (MasterMeterReading.tariff_id — прямая ссылка на запись, сумма в таблице
    считается на лету по r.tariff.rate): менять его ставку/дату или удалять
    нельзя — разошлось бы с уже созданным Expense.amount. Тариф для членов
    показания гаражей копируют числом (ElectricityReading.tariff), его
    правка уже посчитанное не трогает."""
    return tariff is not None and database.db_session.query(MasterMeterReading).filter_by(tariff_id=tariff.id).first() is not None


def _latest_tariff_date() -> dt.date | None:
    latest = database.db_session.query(ElectricityTariff).order_by(ElectricityTariff.effective_date.desc()).first()
    return latest.effective_date if latest else None


@bp.route("/tariff", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_tariff():
    """Новый тариф — сразу обе ставки (поставщика и для членов) с одной
    датой начала: отдельно они не меняются."""
    parsed, error = _parse_tariff_form(request.form)
    if error:
        flash(error, "danger")
        return redirect(url_for("power.view"))
    effective_date, supplier_rate, member_rate, comment = parsed
    existing = _pair_on(effective_date)
    if existing["supplier"] or existing["member"]:
        flash(_("Тариф с этой даты уже есть — измените его."), "danger")
        return redirect(url_for("power.view"))
    database.db_session.add_all([
        ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=supplier_rate, effective_date=effective_date, comment=comment),
        ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=member_rate, effective_date=effective_date, comment=comment),
    ])
    audit.record(
        "power.tariff_add",
        f"Добавлен тариф на электроэнергию с {audit.format_date(effective_date)}: "
        f"поставщика {supplier_rate} ₽/кВт·ч, для членов кооператива {member_rate} ₽/кВт·ч",
    )
    database.db_session.commit()
    flash(_("Тариф добавлен."), "success")
    _flash_member_rate_floor(supplier_rate, member_rate)
    return redirect(url_for("power.view"))


def _tariff_date_or_404(effective):
    try:
        date = dt.date.fromisoformat(effective)
    except ValueError:
        abort(404)
    pair = _pair_on(date)
    if not pair["supplier"] and not pair["member"]:
        abort(404)
    return date, pair


@bp.route("/tariff/<effective>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_tariff(effective):
    """Правка последнего тарифа (обеих ставок). Более ранние уже могли
    использоваться для расчётов. Если тариф поставщика уже попал в
    показания общего счётчика — его ставку и дату не менять (см.
    _supplier_tariff_used), ставку для членов и комментарий — можно."""
    date, pair = _tariff_date_or_404(effective)
    if date != _latest_tariff_date():
        flash(_("Можно изменять только последний добавленный тариф."), "danger")
        return redirect(url_for("power.view"))
    parsed, error = _parse_tariff_form(request.form)
    if error:
        flash(error, "danger")
        return redirect(url_for("power.view"))
    new_date, supplier_rate, member_rate, comment = parsed
    supplier = pair["supplier"]
    if _supplier_tariff_used(supplier) and (supplier.rate != supplier_rate or new_date != date):
        flash(_(
            "Тариф поставщика уже использован в показаниях общего счётчика — его ставку и дату изменить нельзя, "
            "чтобы не исказить уже посчитанные суммы. Ставку для членов кооператива и комментарий изменить можно."
        ), "danger")
        return redirect(url_for("power.view"))
    if new_date != date:
        clash = _pair_on(new_date)
        if clash["supplier"] or clash["member"]:
            flash(_("Тариф с этой даты уже есть — измените его."), "danger")
            return redirect(url_for("power.view"))
    for kind, rate in ((ElectricityTariffKind.SUPPLIER, supplier_rate), (ElectricityTariffKind.MEMBER, member_rate)):
        tariff = pair["supplier" if kind == ElectricityTariffKind.SUPPLIER else "member"]
        if tariff is None:
            tariff = ElectricityTariff(kind=kind)
            database.db_session.add(tariff)
        tariff.rate = rate
        tariff.effective_date = new_date
        tariff.comment = comment
    audit.record(
        "power.tariff_edit",
        f"Изменён тариф на электроэнергию с {audit.format_date(new_date)}: "
        f"поставщика {supplier_rate} ₽/кВт·ч, для членов кооператива {member_rate} ₽/кВт·ч",
    )
    database.db_session.commit()
    flash(_("Тариф изменён."), "success")
    _flash_member_rate_floor(supplier_rate, member_rate)
    return redirect(url_for("power.view"))


@bp.route("/tariff/<effective>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_tariff(effective):
    date, pair = _tariff_date_or_404(effective)
    if date != _latest_tariff_date():
        flash(_("Можно удалить только последний добавленный тариф."), "danger")
        return redirect(url_for("power.view"))
    if _supplier_tariff_used(pair["supplier"]):
        flash(_(
            "Этот тариф уже использован в показаниях общего счётчика — "
            "удаление заблокировано, чтобы не исказить уже посчитанные суммы."
        ), "danger")
        return redirect(url_for("power.view"))
    audit.record("power.tariff_delete", f"Удалён тариф на электроэнергию с {audit.format_date(date)}")
    for tariff in (pair["supplier"], pair["member"]):
        if tariff is not None:
            database.db_session.delete(tariff)
    database.db_session.commit()
    flash(_("Тариф удалён."), "success")
    return redirect(url_for("power.view"))


@bp.route("/readings/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_master_reading():
    f = request.form
    year = int(f["year"])
    month = int(f["month"])

    existing = (
        database.db_session.query(MasterMeterReading)
        .filter_by(year=year, month=month)
        .first()
    )
    if existing:
        flash(_("Запись за {year}-{month:02d} уже существует.", year=year, month=month), "warning")
        return redirect(url_for("power.view"))

    tariff = current_tariff(ElectricityTariffKind.SUPPLIER, dt.date(year, month, 1))
    if tariff is None:
        flash(_("Нет тарифа поставщика, действующего на этот месяц — сначала добавьте тариф."), "danger")
        return redirect(url_for("power.view"))

    statement_line, error = resolve_statement_line(f)
    if error:
        flash(error, "danger")
        return redirect(url_for("power.view"))

    reading = MasterMeterReading(
        year=year,
        month=month,
        reading_date=dt.date(year, month, 1),
        reading=parse_decimal(f["reading"]),
        line_loss=parse_optional_decimal(f.get("line_loss")),
        tariff_id=tariff.id,
        comment=f.get("comment") or None,
    )
    database.db_session.add(reading)
    database.db_session.flush()
    doc = _attach_reading_document(reading, request.files.get("document_file"), f.get("document_title"))

    # Сумму считаем той же логикой, что и таблица показаний на экране
    # (относительно хронологически предыдущего показания, плюс потери в
    # линии) — и заодно наконец сохраняем её в MasterMeterReading.amount
    # (раньше поле не заполнялось, сумма всегда пересчитывалась на лету
    # при отображении).
    all_readings = (
        database.db_session.query(MasterMeterReading)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .all()
    )
    amount = dict((r.id, a) for r, a, _delta in _readings_with_amounts(all_readings)).get(reading.id)
    reading.amount = amount

    # Автоматически заводим расход перед поставщиком на эту сумму (см.
    # accounting.py — Expense/CounterpartyPayment/ExpenseAllocation,
    # зеркало Charge/Payment/ChargeAllocation по лицевым счетам). Если в
    # форме выбран счёт для оплаты — сразу же оплачиваем.
    settings = get_electricity_settings()
    if amount and amount > 0:
        if settings.supplier is None:
            flash(_(
                "Показания внесены, но расход перед поставщиком не создан — "
                "сначала укажите поставщика электроэнергии."
            ), "warning")
            audit.record("power.reading_add", f"Внесены показания общего счётчика за {month}.{year} (без поставщика — расход не создан)")
        else:
            expense = Expense(
                counterparty_id=settings.supplier.id,
                date=reading.reading_date,
                amount=amount,
                category=_("Электроэнергия"),
                description=_("Электроэнергия за {month}.{year} (общий счётчик)", month=month, year=year),
            )
            database.db_session.add(expense)
            database.db_session.flush()
            reading.expense_id = expense.id
            # Document.expense_id, не Expense.document_id — у расхода может
            # быть несколько документов (см. Expense.documents), тот же файл
            # от энергосбыта привязан и к показанию (reading.document_id
            # выше), и к заведённому по нему расходу.
            if doc is not None:
                doc.expense_id = expense.id

            bank_account_id = f.get("bank_account_id")
            bank_account = database.db_session.get(BankAccount, int(bank_account_id)) if bank_account_id else None
            if bank_account is not None or statement_line is not None:
                pay_counterparty(
                    counterparty=settings.supplier,
                    date=reading.reading_date,
                    amount=amount,
                    bank_account=bank_account,
                    comment=_("Оплата за электроэнергию {month}.{year}", month=month, year=year),
                    adjust_balance=bool(f.get("adjust_balance")),
                    bank_statement_line_id=statement_line.id if statement_line else None,
                )
                audit.record(
                    "power.reading_add",
                    f"Внесены показания общего счётчика за {month}.{year}, начислен и сразу оплачен "
                    f"расход перед поставщиком на {audit.format_amount(amount)}",
                    entity_type="counterparty", entity_id=settings.supplier.id,
                )
            else:
                reallocate_counterparty_expenses(settings.supplier)
                audit.record(
                    "power.reading_add",
                    f"Внесены показания общего счётчика за {month}.{year}, начислен расход перед "
                    f"поставщиком на {audit.format_amount(amount)}",
                    entity_type="counterparty", entity_id=settings.supplier.id,
                )
    else:
        audit.record("power.reading_add", f"Внесены показания общего счётчика за {month}.{year} (нулевая сумма начисления)")

    database.db_session.commit()
    flash(_("Показания общего счётчика внесены."), "success")
    return redirect(url_for("power.view"))


@bp.route("/readings/<int:reading_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_master_reading(reading_id):
    """
    Правка — только для последнего (по году/месяцу) показания и только пока
    по связанному расходу перед поставщиком ничего не оплачено (та же логика
    и по той же причине, что и у delete_reading: платёж уже списан со счёта,
    задним числом менять сумму под ним нельзя). Год/месяц/показания/потери/
    комментарий — как при добавлении; тариф выбирается заново по новому
    году/месяцу, сумма и связанный расход (если есть) пересчитываются.
    Смена счёта оплаты/строки выписки в правку не входит — раз показание ещё
    не оплачено, оплата вносится отдельно, в карточке контрагента.
    """
    reading = database.db_session.get(MasterMeterReading, reading_id)
    if reading is None:
        abort(404)

    latest = (
        database.db_session.query(MasterMeterReading)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .first()
    )
    if latest is None or latest.id != reading.id:
        flash(_("Можно редактировать только самое последнее показание."), "danger")
        return redirect(url_for("power.view"))

    if reading.expense_id is not None and expense_paid_amount(reading.expense) > 0:
        flash(_(
            "Нельзя редактировать показание — по связанному расходу перед поставщиком "
            "уже есть оплата. Сначала разберитесь с платежом в карточке контрагента."
        ), "danger")
        return redirect(url_for("power.view"))

    f = request.form
    year = int(f["year"])
    month = int(f["month"])

    duplicate = (
        database.db_session.query(MasterMeterReading)
        .filter(MasterMeterReading.year == year, MasterMeterReading.month == month, MasterMeterReading.id != reading.id)
        .first()
    )
    if duplicate:
        flash(_("Запись за {year}-{month:02d} уже существует.", year=year, month=month), "warning")
        return redirect(url_for("power.view"))

    tariff = current_tariff(ElectricityTariffKind.SUPPLIER, dt.date(year, month, 1))
    if tariff is None:
        flash(_("Нет тарифа поставщика, действующего на этот месяц — сначала добавьте тариф."), "danger")
        return redirect(url_for("power.view"))

    reading.year = year
    reading.month = month
    reading.reading_date = dt.date(year, month, 1)
    reading.reading = parse_decimal(f["reading"])
    reading.line_loss = parse_optional_decimal(f.get("line_loss"))
    reading.tariff_id = tariff.id
    reading.comment = f.get("comment") or None
    database.db_session.flush()

    all_readings = (
        database.db_session.query(MasterMeterReading)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .all()
    )
    amount = dict((r.id, a) for r, a, _delta in _readings_with_amounts(all_readings)).get(reading.id)
    reading.amount = amount

    settings = get_electricity_settings()
    if reading.expense_id is not None:
        expense = reading.expense
        if amount and amount > 0:
            # Расход уже создан и ещё не оплачен (проверено выше) —
            # просто актуализируем сумму/дату под новые показания.
            expense.amount = amount
            expense.date = reading.reading_date
            expense.description = _("Электроэнергия за {month}.{year} (общий счётчик)", month=month, year=year)
            reallocate_counterparty_expenses(expense.counterparty)
        else:
            # Сумма ушла в 0/отрицательную — неоплаченный (paid_amount==0,
            # проверено выше) расход больше не нужен; раз он не оплачен, у
            # него нет собственных ExpenseAllocation, значит удаление не
            # затрагивает распределение платежей по другим расходам этого
            # контрагента — reallocate звать не нужно (как и в delete_reading).
            reading.expense_id = None
            database.db_session.delete(expense)
    elif amount and amount > 0:
        if settings.supplier is None:
            flash(_(
                "Показание изменено, но расход перед поставщиком не создан — "
                "сначала укажите поставщика электроэнергии."
            ), "warning")
        else:
            expense = Expense(
                counterparty_id=settings.supplier.id,
                date=reading.reading_date,
                amount=amount,
                category=_("Электроэнергия"),
                description=_("Электроэнергия за {month}.{year} (общий счётчик)", month=month, year=year),
            )
            database.db_session.add(expense)
            database.db_session.flush()
            reading.expense_id = expense.id
            if reading.document is not None and reading.document.expense_id is None:
                reading.document.expense_id = expense.id
            reallocate_counterparty_expenses(settings.supplier)

    # Файл прикрепляем после пересчёта расхода — чтобы новый документ сразу
    # привязался к актуальному (в т.ч. только что созданному) расходу.
    _attach_reading_document(reading, request.files.get("document_file"), f.get("document_title"))

    audit.record("power.reading_edit", f"Изменено показание общего счётчика за {month}.{year}")
    database.db_session.commit()
    flash(_("Показание изменено."), "success")
    return redirect(url_for("power.view"))


@bp.route("/readings/<int:reading_id>/document", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def attach_master_reading_document(reading_id):
    """
    Прикрепить (или заменить) документ от энергосбыта к ЛЮБОМУ показанию,
    не только к последнему: файл не влияет ни на суммы, ни на цепочку
    показаний, ни на оплату — поэтому ограничений «только последнее / только
    пока не оплачено», как у edit_master_reading, здесь нет.
    """
    reading = database.db_session.get(MasterMeterReading, reading_id)
    if reading is None:
        abort(404)

    file_storage = request.files.get("document_file")
    if not file_storage or not file_storage.filename:
        flash(_("Выберите файл."), "warning")
        return redirect(url_for("power.view"))

    replaced = reading.document_id is not None
    doc = _attach_reading_document(reading, file_storage, request.form.get("document_title"))
    if doc is None:
        flash(_("Недопустимый тип файла."), "danger")
        return redirect(url_for("power.view"))

    audit.record(
        "power.reading_document",
        f"{'Заменён' if replaced else 'Прикреплён'} документ к показанию общего счётчика за {reading.month}.{reading.year}",
    )
    database.db_session.commit()
    flash(_("Документ прикреплён."), "success")
    return redirect(url_for("power.view"))


@bp.route("/readings/<int:reading_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_reading(reading_id):
    """
    Удаление показания. Если по нему уже был создан расход перед
    поставщиком и часть/всё уже оплачено — удаление блокируется: иначе
    реальный платёж (деньги реально списаны со счёта) остался бы висеть
    без объяснения, откуда он взялся. В этом случае сначала нужно
    разобраться с платежом на карточке контрагента (см. reallocate —
    удалять оплаченный расход в обход этого нельзя).
    Если расход есть, но ничего не оплачено — удаляются и показание, и
    сам расход (ExpenseAllocation, если вдруг есть, каскадно удалится
    вместе с ним, см. Expense.allocations в models.py).
    """
    reading = database.db_session.get(MasterMeterReading, reading_id)
    if reading is None:
        abort(404)

    # Показания образуют цепочку: сумма каждого зависит от дельты к
    # предыдущему. Удаление из середины задним числом исказило бы уже
    # сохранённые суммы и расходы всех последующих записей — разрешаем
    # удалять только самое последнее (по году/месяцу) показание.
    latest = (
        database.db_session.query(MasterMeterReading)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .first()
    )
    if latest is None or latest.id != reading.id:
        flash(_(
            "Можно удалить только самое последнее показание — иначе исказятся "
            "суммы уже сохранённых последующих записей."
        ), "danger")
        return redirect(url_for("power.view"))

    if reading.expense_id is not None:
        expense = reading.expense
        if expense_paid_amount(expense) > 0:
            flash(_(
                "Нельзя удалить показание — по связанному расходу перед поставщиком "
                "уже есть оплата. Сначала разберитесь с платежом в карточке контрагента."
            ), "danger")
            return redirect(url_for("power.view"))
        database.db_session.delete(expense)

    audit.record("power.reading_delete", f"Удалено показание общего счётчика за {reading.month}.{reading.year}")
    database.db_session.delete(reading)
    database.db_session.commit()
    flash(_("Показание удалено."), "success")
    return redirect(url_for("power.view"))
