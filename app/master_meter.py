"""Сбор данных с общего (вводного) счётчика кооператива — сверка с гаражными начислениями."""
import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

from . import database
from .auth import login_required, roles_required
from .i18n import translate as _
from .models import MasterMeterReading, ElectricityTariff, Garage, RoleEnum, Cooperative

bp = Blueprint("master_meter", __name__, url_prefix="/electricity")


def _can_view() -> bool:
    """Только правление/председатель/бухгалтер."""
    return g.user.role in (RoleEnum.CHAIRMAN, RoleEnum.BOARD, RoleEnum.ACCOUNTANT)


def _calc_reading_date(year: int, month: int) -> dt.date:
    """Вычисляет дату на первое число месяца."""
    return dt.date(year, month, 1)


@bp.route("/master")
@login_required
def view():
    if not _can_view():
        abort(403)

    readings = (
        database.db_session.query(MasterMeterReading)
        .join(ElectricityTariff)
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .all()
    )
    tariffs = (
        database.db_session.query(ElectricityTariff)
        .order_by(ElectricityTariff.effective_date.desc())
        .all()
    )
    coop = database.db_session.query(Cooperative).first()

    return render_template(
        "master_meter/view.html",
        readings=readings,
        tariffs=tariffs,
        coop=coop,
    )


@bp.route("/master/add", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def add_reading():
    if not _can_view():
        abort(403)

    f = request.form
    year = int(f["year"])
    month = int(f["month"])
    reading_value = Decimal(f["reading"])
    tariff_id = int(f["tariff_id"])
    comment = f.get("comment") or None
    document_id = f.get("document_id", type=int) or None

    # проверка: тариф должен существовать и быть актуальным
    tariff = database.db_session.get(ElectricityTariff, tariff_id)
    if tariff is None:
        flash(_("Указанный тариф не найден."), "danger")
        return redirect(url_for("master_meter.view"))

    # проверка: не дублировать месяц/год
    existing = (
        database.db_session.query(MasterMeterReading)
        .filter_by(year=year, month=month)
        .first()
    )
    if existing:
        flash(_("Запись за {year}-{month:02d} уже существует.", year=year, month=month), "warning")
        return redirect(url_for("master_meter.view"))

    # расчёт суммы — ищем предыдущую запись
    previous = (
        database.db_session.query(MasterMeterReading)
        .filter((MasterMeterReading.year < year) | ((MasterMeterReading.year == year) & (MasterMeterReading.month < month)))
        .order_by(MasterMeterReading.year.desc(), MasterMeterReading.month.desc())
        .first()
    )
    amount = None
    delta = None
    if previous is not None:
        delta = reading_value - previous.reading
        if delta > 0:
            amount = (delta * tariff.rate).quantize(Decimal("0.01"))

    reading_date = _calc_reading_date(year, month)

    reading = MasterMeterReading(
        year=year,
        month=month,
        reading_date=reading_date,
        reading=reading_value,
        tariff_id=tariff_id,
        amount=amount,
        comment=comment,
        document_id=document_id,
    )
    database.db_session.add(reading)
    database.db_session.commit()

    if amount is not None:
        flash(_("Показания внесены. Расход: {delta} кВт·ч, сумма: {amount} ₽.", delta=delta, amount=amount), "success")
    else:
        flash(_("Показания внесены. Сумма не рассчитана — нет предыдущих показаний."), "warning")

    return redirect(url_for("master_meter.view"))


@bp.route("/master/<int:reading_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_reading(reading_id):
    if not _can_view():
        abort(403)

    reading = database.db_session.get(MasterMeterReading, reading_id)
    if reading is None:
        abort(404)
    database.db_session.delete(reading)
    database.db_session.commit()
    flash(_("Запись удалена."), "success")
    return redirect(url_for("master_meter.view"))
