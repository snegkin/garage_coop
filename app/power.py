import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, abort

from . import database
from . import audit
from .i18n import translate as _, parse_decimal, parse_optional_decimal
from .auth import roles_required
from .models import Counterparty, ElectricityTariff, ElectricityTariffKind, MasterMeterReading, Document, DocumentType, RoleEnum, Expense, BankAccount
from .accounting import (
    get_electricity_settings, current_tariff, pay_counterparty, reallocate_counterparty_expenses, expense_paid_amount,
    counterparty_balance, available_statement_lines, resolve_statement_line,
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


def _tariffs_with_range(kind):
    """Список тарифов заданного вида, каждый — с датой окончания действия
    (день перед началом следующего ПО ЭТОМУ ЖЕ ВИДУ, не по обоим сразу —
    у поставщика и у кооператива теперь независимые истории)."""
    tariffs = (
        database.db_session.query(ElectricityTariff)
        .filter(ElectricityTariff.kind == kind)
        .order_by(ElectricityTariff.effective_date.desc())
        .all()
    )
    result = []
    for i, t in enumerate(tariffs):
        end_date = tariffs[i - 1].effective_date - dt.timedelta(days=1) if i > 0 else None
        result.append((t, end_date))
    return result


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    settings = get_electricity_settings()
    supplier_tariffs = _tariffs_with_range(ElectricityTariffKind.SUPPLIER)
    member_tariffs = _tariffs_with_range(ElectricityTariffKind.MEMBER)

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
        supplier_tariff=current_tariff(ElectricityTariffKind.SUPPLIER),
        member_tariff=current_tariff(ElectricityTariffKind.MEMBER),
        supplier_tariffs=supplier_tariffs,
        member_tariffs=member_tariffs,
        readings=readings_with_amounts,
        expense_paid=expense_paid,
        default_reading_year=default_reading_month_date.year,
        default_reading_month=default_reading_month_date.month,
        bank_accounts=bank_accounts,
        counterparties=counterparties,
        supplier_balance=supplier_balance,
        referenceable_statement_lines=available_statement_lines(),
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


@bp.route("/tariff", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_tariff():
    """Один роут на оба вида тарифа (kind из формы) — см. ElectricityTariffKind:
    два разных модальных окна на power/view.html (тариф поставщика / тариф
    для членов) шлют сюда одно и то же с разным скрытым полем kind."""
    f = request.form
    kind = ElectricityTariffKind(f["kind"])
    database.db_session.add(ElectricityTariff(
        kind=kind,
        rate=parse_decimal(f["rate"]),
        effective_date=dt.date.fromisoformat(f["effective_date"]),
        comment=f.get("comment") or None,
    ))
    kind_label = _("поставщика") if kind == ElectricityTariffKind.SUPPLIER else _("для членов кооператива")
    audit.record("power.tariff_add", f"Добавлен тариф на электроэнергию ({kind_label}) {parse_decimal(f['rate'])} ₽/кВт·ч с {audit.format_date(dt.date.fromisoformat(f['effective_date']))}")
    database.db_session.commit()
    flash(_("Тариф добавлен."), "success")
    return redirect(url_for("power.view"))


@bp.route("/tariff/<int:tariff_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_tariff(tariff_id):
    """Править можно только последний (по effective_date) тариф своего вида
    — более ранние уже могли использоваться для расчётов, и правка задним
    числом исказила бы уже сохранённые суммы. Для тарифа поставщика
    дополнительно проверяем, что на него ещё не ссылается ни одно
    показание общего счётчика (MasterMeterReading.tariff_id — прямая
    ссылка на запись, в отличие от тарифа для членов, который читатели
    показаний копируют числом, см. garages.add_meter_reading): иначе
    показанная в таблице сумма (считается на лету по r.tariff.rate)
    разошлась бы с уже созданным Expense.amount, зафиксированным при
    создании показания."""
    tariff = database.db_session.get(ElectricityTariff, tariff_id)
    if tariff is None:
        abort(404)

    latest = (
        database.db_session.query(ElectricityTariff)
        .filter_by(kind=tariff.kind)
        .order_by(ElectricityTariff.effective_date.desc(), ElectricityTariff.id.desc())
        .first()
    )
    if latest is None or latest.id != tariff.id:
        flash(_("Можно редактировать только последний добавленный тариф этого вида."), "danger")
        return redirect(url_for("power.view"))

    if tariff.kind == ElectricityTariffKind.SUPPLIER:
        used = (
            database.db_session.query(MasterMeterReading)
            .filter_by(tariff_id=tariff.id)
            .first()
        )
        if used is not None:
            flash(_(
                "Этот тариф уже использован в показаниях общего счётчика — "
                "редактирование заблокировано, чтобы не исказить уже посчитанные суммы."
            ), "danger")
            return redirect(url_for("power.view"))

    f = request.form
    tariff.rate = parse_decimal(f["rate"])
    tariff.effective_date = dt.date.fromisoformat(f["effective_date"])
    tariff.comment = f.get("comment") or None
    kind_label = _("поставщика") if tariff.kind == ElectricityTariffKind.SUPPLIER else _("для членов кооператива")
    audit.record("power.tariff_edit", f"Изменён тариф на электроэнергию ({kind_label}): {tariff.rate} ₽/кВт·ч с {audit.format_date(tariff.effective_date)}")
    database.db_session.commit()
    flash(_("Тариф изменён."), "success")
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

    document_id = None
    file_path = save_upload(request.files.get("document_file"), current_app.config["UPLOAD_FOLDER"])
    if file_path:
        title = f.get("document_title") or _(
            "Счёт за электроэнергию {month}.{year}", month=month, year=year
        )
        doc = Document(doc_type=DocumentType.LETTER, date=dt.date.today(), title=title, file_path=file_path)
        database.db_session.add(doc)
        database.db_session.flush()
        document_id = doc.id

    reading = MasterMeterReading(
        year=year,
        month=month,
        reading_date=dt.date(year, month, 1),
        reading=parse_decimal(f["reading"]),
        line_loss=parse_optional_decimal(f.get("line_loss")),
        tariff_id=tariff.id,
        comment=f.get("comment") or None,
        document_id=document_id,
    )
    database.db_session.add(reading)
    database.db_session.flush()

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
            if document_id is not None:
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
