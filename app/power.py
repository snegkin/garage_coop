import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app

from . import database
from .i18n import translate as _
from .auth import roles_required
from .models import Counterparty, ElectricityTariff, MasterMeterReading, Document, DocumentType, RoleEnum
from .accounting import get_electricity_settings, current_tariff
from .uploads import save_upload

bp = Blueprint("power", __name__, url_prefix="/power")


def _readings_with_amounts(readings_desc):
    """
    readings_desc — список MasterMeterReading, отсортированный по (год, месяц) по убыванию.
    Возвращает список (reading, amount): amount = (текущие показания − предыдущие) × тариф,
    None для самой первой по времени записи (не с чем сравнивать).
    """
    chronological = list(reversed(readings_desc))
    result = []
    previous = None
    for r in chronological:
        if previous is None:
            amount = None
        else:
            delta = r.reading - previous.reading
            amount = (delta * r.tariff.rate).quantize(Decimal("0.01")) if delta > 0 else Decimal("0.00")
        result.append((r, amount))
        previous = r
    result.reverse()
    return result


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    settings = get_electricity_settings()
    tariffs = database.db_session.query(ElectricityTariff).order_by(ElectricityTariff.effective_date.desc()).all()

    # для каждого тарифа — дата окончания действия (день перед началом следующего по дате)
    tariffs_with_range = []
    for i, t in enumerate(tariffs):
        end_date = tariffs[i - 1].effective_date - dt.timedelta(days=1) if i > 0 else None
        tariffs_with_range.append((t, end_date))

    readings = (
        database.db_session.query(MasterMeterReading)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .all()
    )
    return render_template(
        "power/view.html",
        settings=settings,
        tariff=current_tariff(),
        tariffs=tariffs_with_range,
        readings=_readings_with_amounts(readings),
    )


@bp.route("/supplier", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def save_supplier():
    settings = get_electricity_settings()
    f = request.form

    supplier = settings.supplier
    if supplier is None:
        supplier = Counterparty(name="")
        database.db_session.add(supplier)
        database.db_session.flush()
        settings.supplier_id = supplier.id

    supplier.name = f["name"]
    supplier.inn = f.get("inn") or None
    supplier.kpp = f.get("kpp") or None
    supplier.phone = f.get("phone") or None
    supplier.email = f.get("email") or None
    supplier.address = f.get("address") or None
    supplier.comment = f.get("comment") or None
    supplier.category = "электроснабжение"
    database.db_session.commit()
    flash(_("Данные поставщика сохранены."), "success")
    return redirect(url_for("power.view"))


@bp.route("/tariff", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_tariff():
    f = request.form
    database.db_session.add(ElectricityTariff(
        rate=Decimal(f["rate"]),
        effective_date=dt.date.fromisoformat(f["effective_date"]),
        comment=f.get("comment") or None,
    ))
    database.db_session.commit()
    flash(_("Тариф добавлен."), "success")
    return redirect(url_for("power.view"))


@bp.route("/readings/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_master_reading():
    f = request.form
    year = int(f["year"])
    month = int(f["month"])

    tariff = current_tariff(dt.date(year, month, 1))
    if tariff is None:
        flash(_("Нет тарифа, действующего на этот месяц — сначала добавьте тариф."), "danger")
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

    database.db_session.add(MasterMeterReading(
        year=year,
        month=month,
        reading=Decimal(f["reading"]),
        tariff_id=tariff.id,
        comment=f.get("comment") or None,
        document_id=document_id,
    ))
    database.db_session.commit()
    flash(_("Показания общего счётчика внесены."), "success")
    return redirect(url_for("power.view"))
