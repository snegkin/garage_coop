import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

from . import database
from .auth import login_required, roles_required
from .i18n import translate as _
from .models import Garage, ElectricityMeter, ElectricityReading, Charge, FeeType, RoleEnum
from .accounting import current_tariff

bp = Blueprint("electricity", __name__, url_prefix="/garages/<int:garage_id>/electricity")


def _current_meter(garage: Garage) -> ElectricityMeter | None:
    """Актуальный счётчик — последняя по дате установки (а если равны — по id) запись."""
    if not garage.meters:
        return None
    return sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id))[-1]


def _can_view_garage(garage: Garage) -> bool:
    """Правление/председатель видят всё; рядовой член — только свои гаражи (по владению)."""
    if g.user.role in (RoleEnum.CHAIRMAN, RoleEnum.BOARD, RoleEnum.ACCOUNTANT):
        return True
    if g.user.person_id is None:
        return False
    owner_ids = {o.person_id for o in garage.ownerships}
    return g.user.person_id in owner_ids


@bp.route("/")
@login_required
def view(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _can_view_garage(garage):
        abort(403)
    current = _current_meter(garage)
    history = sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id), reverse=True)
    readings = sorted(current.readings, key=lambda r: r.reading_date, reverse=True) if current else []
    return render_template(
        "electricity/view.html", garage=garage, current=current, history=history, readings=readings
    )


@bp.route("/meters/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_meter(garage_id):
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
    return redirect(url_for("electricity.view", garage_id=garage.id))


@bp.route("/readings/add", methods=["POST"])
@login_required
def add_reading(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _can_view_garage(garage):
        abort(403)

    current = _current_meter(garage)
    if current is None:
        flash(_("Сначала добавьте счётчик."), "danger")
        return redirect(url_for("electricity.view", garage_id=garage.id))

    f = request.form
    reading_value = Decimal(f["reading"])
    reading_date = dt.date.fromisoformat(f["reading_date"])

    # предыдущее показание этого счётчика (по дате) — или начальные показания счётчика, если это первая запись
    previous = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    baseline = previous.reading if previous else current.initial_reading
    amount = None
    if baseline is not None:
        delta = reading_value - baseline
        tariff = current_tariff(reading_date)
        if tariff is not None and delta > 0:
            amount = (delta * tariff.rate).quantize(Decimal("0.01"))

    reading = ElectricityReading(
        meter_id=current.id,
        reading=reading_value,
        reading_date=reading_date,
        amount=amount,
        comment=f.get("comment") or None,
    )
    database.db_session.add(reading)

    if amount is not None:
        electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
        if electricity_fee_type is not None:
            database.db_session.add(Charge(
                garage_id=garage.id,
                fee_type_id=electricity_fee_type.id,
                year=reading_date.year,
                amount=amount,
                comment=_("Начислено по показаниям от {date}", date=reading_date.isoformat()),
            ))

    database.db_session.commit()

    if amount is None:
        flash(_("Показания внесены. Сумма не рассчитана — задайте тариф на странице «Электроэнергия»."), "warning")
    else:
        flash(_("Показания внесены, начислено {amount} ₽.", amount=amount), "success")
    return redirect(url_for("electricity.view", garage_id=garage.id))
