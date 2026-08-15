import datetime as dt
import os
import uuid
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, g, abort,
    current_app, send_from_directory,
)

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import (
    Garage, Person, GarageOwnership, GarageContact, GaragePhoto, PersonalAccount,
    MemberAccount, FeeType, RoleEnum, ElectricityMeter, ElectricityReading,
    Charge, Payment, User,
)
from .accounting import electricity_account_number, member_account_number, balance, current_tariff, reallocate_garage_charges, charge_paid_amount

bp = Blueprint("garages", __name__, url_prefix="/garages")


def _is_owner_or_board(garage: Garage) -> bool:
    """Правление/председатель — любой гараж; рядовой член — только свой (по владению)."""
    if g.user.role in (RoleEnum.CHAIRMAN, RoleEnum.BOARD, RoleEnum.ACCOUNTANT):
        return True
    if g.user.person_id is None:
        return False
    owner_ids = {o.person_id for o in garage.ownerships}
    return g.user.person_id in owner_ids


def _current_meter(garage: Garage):
    """Актуальный счётчик — последняя по дате установки запись."""
    if not garage.meters:
        return None
    return sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id))[-1]


def _meter_history(garage: Garage):
    """Все счётчики, отсортированные от новых к старым."""
    return sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id), reverse=True)


def _ensure_member_accounts(garage: Garage, person_id: int, owner_index: int):
    """
    Заводит члену кооператива лицевые счета на все виды взносов/налогов,
    для которых задан type_code (см. FeeType), по этому гаражу — если их
    ещё нет. Электричество сюда не входит — у него отдельный счёт на гараж.
    """
    fee_types = (
        database.db_session.query(FeeType)
        .filter(FeeType.type_code.isnot(None))
        .all()
    )
    for fee_type in fee_types:
        exists = (
            database.db_session.query(MemberAccount)
            .filter_by(person_id=person_id, garage_id=garage.id, fee_type_id=fee_type.id)
            .first()
        )
        if exists:
            continue
        number = member_account_number(fee_type.type_code, garage.number, owner_index, fee_type.is_penalty)
        database.db_session.add(MemberAccount(
            person_id=person_id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=number,
        ))


@bp.route("/")
@login_required
def list_garages():
    garages = database.db_session.query(Garage).order_by(Garage.number).all()
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    preselect_person_id = request.args.get("new_person_id", type=int)
    balances = {garage.id: (balance(garage) if garage else None) for garage in garages}
    return render_template(
        "garages/list.html", garages=garages, all_persons=all_persons,
        preselect_person_id=preselect_person_id, balances=balances,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        garage = Garage(
            number=f["number"],
            area_sqm=Decimal(f["area_sqm"]),
            coefficient=Decimal(f["coefficient"]) if f.get("coefficient") else Decimal("1"),
            land_privatized=bool(f.get("land_privatized")),
            cadastral_number=f.get("cadastral_number") or None,
            land_cadastral_number=f.get("land_cadastral_number") or None,
            privatized_land_area=Decimal(f["privatized_land_area"]) if f.get("privatized_land_area") else None,
            comment=f.get("comment") or None,
        )
        database.db_session.add(garage)
        database.db_session.flush()  # чтобы получить garage.id

        # лицевой счёт на электричество заводится автоматически вместе с гаражом
        account = PersonalAccount(garage_id=garage.id, account_number=electricity_account_number(garage.number))
        database.db_session.add(account)

        # собственники, указанные прямо в форме создания
        person_ids = request.form.getlist("owner_person_id")
        shares = request.form.getlist("owner_share") or "1"
        owner_index = 0
        for person_id, share_raw in zip(person_ids, shares):
            if not person_id or not share_raw:
                continue
            try:
                share = Decimal(share_raw)
            except InvalidOperation:
                continue
            if not (0 < share <= 1):
                continue
            database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=int(person_id), share=share))
            database.db_session.flush()
            _ensure_member_accounts(garage, int(person_id), owner_index)
            owner_index += 1

        # фото гаража (необязательно)
        upload = request.files.get("photo")
        if upload and upload.filename:
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext in ALLOWED_PHOTO_EXT:
                stored_name = f"{uuid.uuid4().hex}{ext}"
                upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))
                database.db_session.add(GaragePhoto(garage_id=garage.id, file_path=stored_name))
            else:
                flash(_("Фото не сохранено: поддерживаются только изображения (jpg, png, webp, gif)."), "warning")

        database.db_session.commit()
        flash(_("Гараж №{number} создан, лицевой счёт открыт.", number=garage.number), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id))

    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    preselect_person_id = request.args.get("new_person_id", type=int)
    return render_template(
        "garages/form.html", garage=None, all_persons=all_persons, preselect_person_id=preselect_person_id
    )


@bp.route("/<int:garage_id>")
@login_required
def detail(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages"))
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    total_share = sum((o.share for o in garage.ownerships), Decimal("0"))
    preselect_contact_person_id = request.args.get("new_person_id", type=int)

    # электричество
    current_meter = _current_meter(garage)
    meter_history = _meter_history(garage)
    readings = sorted(current_meter.readings, key=lambda r: r.reading_date, reverse=True) if current_meter else []

    # электросчёт
    account = garage.account
    acc_balance = balance(account.garage) if account else None
    charges = sorted(account.garage.charges, key=lambda c: c.year, reverse=True) if account else []
    payments = sorted(account.garage.payments, key=lambda p: p.date, reverse=True) if account else []
    fee_types_list = database.db_session.query(FeeType).order_by(FeeType.name).all()

    # объединённая таблица: показание берёт своё начисление напрямую через связь
    # Charge.reading (FK reading_id, а не текстовым сопоставлением); начисления без
    # привязки к показанию (ручные, любого вида взноса) идут отдельными строками той
    # же таблицы. Платежи, закрывающие начисление (через ChargeAllocation), показаны
    # в столбцах «От кого/Дата оплаты/Сумма» ЭТОЙ ЖЕ строки начисления, а не отдельной
    # строкой — так по одному начислению не возникает лишней строки на каждый платёж.
    # Отдельной строкой платёж идёт только на ту часть суммы, которая не пошла ни на
    # одно начисление (аванс/переплата).
    ledger_rows = []

    def _charge_payments(charge_obj):
        return [{
            "payer": a.payment.payer.full_name if a.payment.payer else None,
            "date": a.payment.date,
            "amount": a.amount,
        } for a in sorted(charge_obj.allocations, key=lambda a: a.payment.date)]

    for r in readings:
        charge_obj = r.charge
        ledger_rows.append({
            "sort_date": r.reading_date,
            "reading_date": r.reading_date,
            "reading": r.reading,
            "tariff": r.tariff,
            "charge_amount": charge_obj.amount if charge_obj else None,
            "charge_paid": charge_paid_amount(charge_obj) if charge_obj else None,
            "payments": _charge_payments(charge_obj) if charge_obj else [],
        })
    for c in charges:
        if c.reading_id is not None:
            continue  # уже отражено вместе со своим показанием выше
        ledger_rows.append({
            "sort_date": dt.date(c.year, 1, 1),
            "reading_date": None,
            "reading": None,
            "tariff": None,
            "charge_amount": c.amount,
            "charge_paid": charge_paid_amount(c),
            "charge_label": c.fee_type.name if c.fee_type else None,
            "payments": _charge_payments(c),
        })
    for p in payments:
        allocated = sum((a.amount for a in p.allocations), Decimal("0"))
        unallocated = p.amount - allocated
        if unallocated > 0:
            ledger_rows.append({
                "sort_date": p.date,
                "reading_date": None,
                "reading": None,
                "tariff": None,
                "charge_amount": None,
                "charge_label": _("аванс") if allocated > 0 else None,
                "payments": [{"payer": p.payer.full_name if p.payer else None, "date": p.date, "amount": unallocated}],
            })
    ledger_rows.sort(key=lambda row: row["sort_date"], reverse=True)

    # значения по умолчанию для формы «Зарегистрировать платёж»: сегодняшняя дата,
    # плательщик — тот из собственников, кто платил последним (сортировка списка
    # по дате последнего платежа, от нового к старому), сумма — текущая задолженность
    today = dt.date.today()
    last_payment_by_person = {}
    for p in payments:
        if p.payer_person_id:
            last_payment_by_person[p.payer_person_id] = max(
                last_payment_by_person.get(p.payer_person_id, p.date), p.date
            )
    owners_sorted = sorted(
        garage.ownerships,
        key=lambda o: last_payment_by_person.get(o.person_id, dt.date.min),
        reverse=True,
    )
    default_payer_id = owners_sorted[0].person_id if owners_sorted else None
    default_payment_amount = (
        str((-acc_balance).quantize(Decimal("0.01"))) if acc_balance is not None and acc_balance < 0 else ""
    )

    return render_template(
        "garages/detail.html",
        garage=garage,
        all_persons=all_persons,
        total_share=total_share,
        preselect_contact_person_id=preselect_contact_person_id,
        current_meter=current_meter,
        meter_history=meter_history,
        readings=readings,
        ledger_rows=ledger_rows,
        account=account,
        account_balance=acc_balance,
        charges=charges,
        payments=payments,
        fee_types=fee_types_list,
        today=today,
        owners_sorted=owners_sorted,
        default_payer_id=default_payer_id,
        default_payment_amount=default_payment_amount,
    )


@bp.route("/<int:garage_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages"))

    if request.method == "POST":
        f = request.form
        garage.number = f["number"]
        garage.area_sqm = Decimal(f["area_sqm"])
        garage.coefficient = Decimal(f["coefficient"]) if f.get("coefficient") else Decimal("1")
        garage.land_privatized = bool(f.get("land_privatized"))
        garage.cadastral_number = f.get("cadastral_number") or None
        garage.land_cadastral_number = f.get("land_cadastral_number") or None
        garage.privatized_land_area = Decimal(f["privatized_land_area"]) if f.get("privatized_land_area") else None
        garage.comment = f.get("comment") or None
        database.db_session.commit()
        flash(_("Изменения сохранены."), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id))

    return render_template("garages/form.html", garage=garage, all_persons=[], preselect_person_id=None)


@bp.route("/<int:garage_id>/owners/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_owner(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    person_id = int(request.form["person_id"])
    try:
        share = Decimal(request.form["share"] or "1")
    except InvalidOperation:
        flash(_("Доля должна быть числом (например 0.5)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    if not (0 < share <= 1):
        flash(_("Доля должна быть в диапазоне от 0 (не включая) до 1."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    existing = (
        database.db_session.query(GarageOwnership)
        .filter_by(garage_id=garage.id, person_id=person_id)
        .first()
    )
    if existing:
        existing.share = share
    else:
        owner_index = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).count()
        database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=person_id, share=share))
        database.db_session.flush()
        _ensure_member_accounts(garage, person_id, owner_index)
    database.db_session.commit()
    flash(_("Собственник добавлен/обновлён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/owners/<int:ownership_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def remove_owner(garage_id, ownership_id):
    ownership = database.db_session.get(GarageOwnership, ownership_id)
    if ownership and ownership.garage_id == garage_id:
        database.db_session.delete(ownership)
        database.db_session.commit()
        flash(_("Собственник удалён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/contacts/add", methods=["POST"])
@login_required
def add_contact(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_owner_or_board(garage):
        abort(403)
    person_id = int(request.form["person_id"])
    relation = request.form.get("relation") or None
    database.db_session.add(GarageContact(garage_id=garage_id, person_id=person_id, relation=relation))
    database.db_session.commit()
    flash(_("Контактное лицо добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/contacts/<int:contact_id>/remove", methods=["POST"])
@login_required
def remove_contact(garage_id, contact_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_owner_or_board(garage):
        abort(403)
    contact = database.db_session.get(GarageContact, contact_id)
    if contact and contact.garage_id == garage_id:
        database.db_session.delete(contact)
        database.db_session.commit()
        flash(_("Контактное лицо удалено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/comment", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_comment(garage_id):
    """Комментарий к гаражу видит и меняет только правление — это внутренние заметки, не для собственников."""
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    garage.comment = request.form.get("comment") or None
    database.db_session.commit()
    flash(_("Комментарий обновлён."), "success")
    return redirect(request.referrer or url_for("garages.detail", garage_id=garage_id))


# ---------------------------------------------------------------------------
# Фото гаража
# ---------------------------------------------------------------------------

ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@bp.route("/<int:garage_id>/photos/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_photo(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash(_("Выберите файл фотографии."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_PHOTO_EXT:
        flash(_("Поддерживаются только изображения (jpg, png, webp, gif)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    stored_name = f"{uuid.uuid4().hex}{ext}"
    upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))

    photo = GaragePhoto(garage_id=garage.id, file_path=stored_name, caption=request.form.get("caption") or None)
    database.db_session.add(photo)
    database.db_session.commit()
    flash(_("Фото добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/photos/<int:photo_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_photo(garage_id, photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None or photo.garage_id != garage_id:
        abort(404)

    photo.caption = request.form.get("caption") or None

    upload = request.files.get("file")
    if upload and upload.filename:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ALLOWED_PHOTO_EXT:
            flash(_("Поддерживаются только изображения (jpg, png, webp, gif)."), "danger")
            return redirect(url_for("garages.detail", garage_id=garage_id))
        old_path = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
        if os.path.exists(old_path):
            os.remove(old_path)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))
        photo.file_path = stored_name

    database.db_session.commit()
    flash(_("Фото обновлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/photos/<int:photo_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def remove_photo(garage_id, photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None or photo.garage_id != garage_id:
        abort(404)

    file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
    if os.path.exists(file_path):
        os.remove(file_path)
    database.db_session.delete(photo)
    database.db_session.commit()
    flash(_("Фото удалено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/photos/<int:photo_id>/file")
@login_required
def photo_file(photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], photo.file_path)


# ---------------------------------------------------------------------------
# Электричество: счётчики и показания
# ---------------------------------------------------------------------------

@bp.route("/<int:garage_id>/electricity/meter/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_electricity_meter(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    f = request.form
    installed = f.get("installed_date")
    sealed = f.get("sealed_date")
    initial_reading = f.get("initial_reading")

    meter = ElectricityMeter(
        garage_id=garage.id,
        meter_number=f["meter_number"],
        installed_date=dt.date.fromisoformat(installed) if installed else None,
        sealed_date=dt.date.fromisoformat(sealed) if sealed else None,
        initial_reading=Decimal(initial_reading) if initial_reading else None,
        meter_seal_number=f.get("meter_seal_number") or None,
        breaker_seal_number=f.get("breaker_seal_number") or None,
        comment=f.get("comment") or None,
    )
    database.db_session.add(meter)
    database.db_session.commit()
    flash(_("Счётчик добавлен."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/electricity/reading/add", methods=["POST"])
@login_required
def add_electricity_reading(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_owner_or_board(garage):
        abort(403)

    current = _current_meter(garage)
    if current is None:
        flash(_("Сначала добавьте счётчик."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    f = request.form
    reading_value = Decimal(f["reading"])
    reading_date = dt.date.today()

    # предыдущее показание этого счётчика (по дате) — или начальные показания счётчика, если это первая запись
    previous = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    baseline = previous.reading if previous else current.initial_reading
    if baseline is not None and reading_value <= baseline:
        flash(_(
            "Показания не могут быть меньше предыдущих ({baseline}). Если счётчик был заменён, сначала внесите новый прибор учёта.",
            baseline=str(baseline.quantize(Decimal("0.01"))),
        ), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    amount = None
    tariff = current_tariff(reading_date)
    tariff_rate = tariff.rate if tariff is not None else None
    if baseline is not None:
        delta = reading_value - baseline
        if tariff is not None and delta > 0:
            amount = (delta * tariff.rate).quantize(Decimal("0.01"))

    reading = ElectricityReading(
        meter_id=current.id,
        reading=reading_value,
        reading_date=reading_date,
        amount=amount,
        tariff=tariff_rate,
        comment=f.get("comment") or None,
    )
    database.db_session.add(reading)
    database.db_session.flush()  # получить reading.id для связи с начислением

    if amount is not None:
        electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
        if electricity_fee_type is not None:
            database.db_session.add(Charge(
                garage_id=garage.id,
                fee_type_id=electricity_fee_type.id,
                year=reading_date.year,
                amount=amount,
                reading_id=reading.id,
                comment=_("Начислено по показаниям от {date}", date=reading_date.isoformat()),
            ))
            database.db_session.flush()
            reallocate_garage_charges(garage)

    database.db_session.commit()

    if amount is None:
        flash(_("Показания внесены. Сумма не рассчитана — задайте тариф на странице «Электроэнергия»."), "warning")
    else:
        flash(_("Показания внесены, начислено {amount} ₽.", amount=amount), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/electricity/readings/last/edit", methods=["POST"])
@login_required
def edit_last_reading(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if g.user.role.value != "chairman":
        abort(403)

    current = _current_meter(garage)
    if current is None:
        abort(404)

    last_reading = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    if last_reading is None:
        abort(404)

    f = request.form
    try:
        new_value = Decimal(f["reading"])
    except InvalidOperation:
        flash(_("Некорректное значение показаний."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    previous = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .filter(ElectricityReading.id != last_reading.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    baseline = previous.reading if previous else current.initial_reading

    if baseline is not None and new_value < baseline:
        flash(_(
            "Показания не могут быть меньше предыдущих ({baseline}).",
            baseline=str(baseline.quantize(Decimal("0.01"))),
        ), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    tariff_rate = last_reading.tariff
    if tariff_rate is None:
        tariff = current_tariff(last_reading.reading_date)
        tariff_rate = tariff.rate if tariff is not None else None

    amount = None
    if baseline is not None and tariff_rate is not None:
        delta = new_value - baseline
        if delta > 0:
            amount = (delta * tariff_rate).quantize(Decimal("0.01"))

    last_reading.reading = new_value
    last_reading.tariff = tariff_rate
    if f.get("comment") is not None:
        last_reading.comment = f.get("comment") or None

    charge = last_reading.charge
    if amount is not None:
        if charge is not None:
            charge.amount = amount
        else:
            electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
            if electricity_fee_type is not None:
                database.db_session.add(Charge(
                    garage_id=garage.id,
                    fee_type_id=electricity_fee_type.id,
                    year=last_reading.reading_date.year,
                    amount=amount,
                    reading_id=last_reading.id,
                    comment=_("Начислено по показаниям от {date}", date=last_reading.reading_date.isoformat()),
                ))
    elif charge is not None:
        garage.charges.remove(charge)

    database.db_session.flush()
    reallocate_garage_charges(garage)
    database.db_session.commit()
    flash(_("Последнее показание исправлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


# ---------------------------------------------------------------------------
# Номер счёта
# ---------------------------------------------------------------------------

@bp.route("/<int:garage_id>/account/number", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_account_number(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    account = garage.account
    if account is None:
        abort(404)
    account.account_number = request.form["account_number"].strip()
    try:
        database.db_session.commit()
    except Exception:
        database.db_session.rollback()
        flash(_("Такой номер счёта уже используется."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))
    flash(_("Номер счёта обновлён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


# ---------------------------------------------------------------------------
# Гаражные начисления и платежи
# ---------------------------------------------------------------------------

@bp.route("/charges/add", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def add_charge_page():
    if request.method == "POST":
        f = request.form
        garage = database.db_session.get(Garage, int(f["garage_id"]))
        if garage is None:
            abort(404)
        fee_type = database.db_session.get(FeeType, int(f["fee_type_id"]))
        if fee_type is None:
            abort(404)
        charge = Charge(
            garage_id=garage.id,
            fee_type_id=fee_type.id,
            year=int(f["year"]),
            amount=Decimal(f["amount"]),
            comment=f.get("comment") or None,
        )
        database.db_session.add(charge)
        database.db_session.flush()
        reallocate_garage_charges(garage)
        database.db_session.commit()
        flash(_("Начисление добавлено."), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id, tab="account"))

    garages_list = database.db_session.query(Garage).order_by(Garage.number).all()
    fee_types_list = database.db_session.query(FeeType).order_by(FeeType.name).all()
    person_names = {
        garage.id: ", ".join(o.person.full_name for o in garage.ownerships)
        for garage in garages_list
    }
    return render_template(
        "garages/add_charge.html",
        garages=garages_list,
        fee_types=fee_types_list,
        person_names=person_names,
        current_year=dt.date.today().year,
    )


@bp.route("/<int:garage_id>/charges/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_charge(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    f = request.form
    charge = Charge(
        garage_id=garage.id,
        fee_type_id=int(f["fee_type_id"]),
        year=int(f["year"]),
        amount=Decimal(f["amount"]),
    )
    database.db_session.add(charge)
    database.db_session.flush()
    reallocate_garage_charges(garage)
    database.db_session.commit()
    flash(_("Начисление добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/payments/add", methods=["POST"])
@login_required
def add_payment(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_owner_or_board(garage):
        abort(403)

    f = request.form
    payment = Payment(
        garage_id=garage.id,
        date=dt.date.fromisoformat(f["date"]),
        amount=Decimal(f["amount"]),
        payer_person_id=int(f["payer_person_id"]) if f.get("payer_person_id") else None,
        comment=f.get("comment") or None,
    )
    database.db_session.add(payment)
    database.db_session.flush()
    reallocate_garage_charges(garage)
    database.db_session.commit()
    flash(_("Платёж зарегистрирован."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))
