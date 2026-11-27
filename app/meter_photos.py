"""Показания счётчиков по фото.

Председатель на обходе (или собственник у себя в гараже) фотографирует
счётчик телефоном прямо на сайте. Сайт находит на снимке надписи (OCR —
отдельным процессом, app/meter_ocr.py), узнаёт по номеру счётчика гараж
(app/meter_recognition.py) и предлагает показания. Человек проверяет их и
подтверждает одной кнопкой: показания вносятся с начислением так же, как
формой «Внести показания» на карточке гаража
(garages.record_electricity_reading), а фото остаётся при показаниях.

Кто что может — как с формой на карточке гаража: правление — любой гараж
и задним числом (дата съёмки из EXIF подставляется сама), собственник и
лицо для связи — свои гаражи и на сегодня. У собственника нескольких
гаражей гараж узнаётся по номеру счётчика на фото.

Распознавание необязательно: если OCR на сервере не установлен
(rapidocr) или не справился, фото всё равно сохраняется, а показания
вводятся вручную.
"""
import datetime as dt
import json
import os
import subprocess
import threading
import uuid
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, send_from_directory, url_for

from . import database
from .auth import login_required
from .garages import ReadingError, _current_meter, latest_reading, record_electricity_reading
from .i18n import parse_decimal, translate as _
from .images import shrink_image
from .meter_recognition import digits_only, extract_numbers, match_meter, reading_options
from .models import (
    ElectricityReading, Garage, GarageContact, GarageOwnership, MeterPhoto, garage_sort_key,
)
from .permissions import is_board, is_owner_or_board
from .rate_limit import limiter

bp = Blueprint("meter_photos", __name__, url_prefix="/electricity/photo")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
OCR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter_ocr.py")
# Один снимок за раз на процесс сайта: распознавание занимает оба ядра
# сервера на несколько секунд, параллельные только мешали бы друг другу.
_ocr_lock = threading.Lock()
# Неподтверждённые фото человека старше этого удаляются при его следующей загрузке.
STALE_UNCONFIRMED = dt.timedelta(days=1)

WARNING_TEXTS = {
    "not_above_previous": "не больше предыдущих",
    "too_high": "слишком большой расход",
}


def _warning_text(code):
    return _(WARNING_TEXTS[code]) if code in WARNING_TEXTS else code


def available_garages():
    """Гаражи с текущим счётчиком, по которым человек может вносить
    показания (те же права, что permissions.is_owner_or_board)."""
    session = database.db_session
    if is_board():
        garages = session.query(Garage).all()
    elif g.user.person_id is None:
        garages = []
    else:
        ids = {o.garage_id for o in session.query(GarageOwnership).filter_by(person_id=g.user.person_id)}
        ids |= {c.garage_id for c in session.query(GarageContact).filter_by(person_id=g.user.person_id)}
        garages = session.query(Garage).filter(Garage.id.in_(ids)).all() if ids else []
    result = [garage for garage in garages if _current_meter(garage) is not None]
    return sorted(result, key=garage_sort_key)


def run_ocr(path):
    """Надписи на фото: dict из app/meter_ocr.py или {"error": ...}.
    Python для OCR — METER_OCR_PYTHON (можно отдельный venv), по умолчанию
    тот же, что у сайта."""
    python = current_app.config.get("METER_OCR_PYTHON")
    if not python:
        return {"error": "OCR disabled"}
    timeout = current_app.config.get("METER_OCR_TIMEOUT", 60)
    with _ocr_lock:
        try:
            proc = subprocess.run(
                [python, "-I", OCR_SCRIPT, path], capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"{type(e).__name__}: {e}"}
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (ValueError, IndexError):
        out = {}
    if proc.returncode != 0 or "tokens" not in out:
        detail = out.get("error") or (proc.stderr or "").strip().splitlines()[-1:] or ["?"]
        return {"error": detail if isinstance(detail, str) else detail[0]}
    return out


def _avg_daily(meter):
    """Обычный расход счётчика, кВт·ч/сутки — по показаниям за последний
    год (последнее показание против самого раннего в пределах ~400 дней,
    а если показание одно — против показаний при установке)."""
    readings = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=meter.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .all()
    )
    if not readings:
        return None
    last = readings[0]
    start = None
    for r in readings[1:]:
        if (last.reading_date - r.reading_date).days > 400:
            break
        start = (r.reading_date, r.reading)
    if start is None and meter.installed_date is not None and meter.initial_reading is not None:
        start = (meter.installed_date, meter.initial_reading)
    if start is None:
        return None
    days = (last.reading_date - start[0]).days
    if days <= 0 or last.reading <= start[1]:
        return None
    return (last.reading - start[1]) / days


def analyze(photo, garages, garage_id=None):
    """Что показать человеку по фото: гараж, номер, предыдущие показания и
    варианты показаний. garage_id — гараж, выбранный вручную (иначе — по
    номеру на фото, а если гараж у человека один — он)."""
    tokens = json.loads(photo.ocr_tokens) if photo.ocr_tokens else []
    numbers = extract_numbers(tokens)
    by_id = {garage.id: garage for garage in garages}
    meters = {garage.id: _current_meter(garage) for garage in garages}
    match = match_meter(numbers, [(m.id, m.meter_number) for m in meters.values()])
    matched_garage = next((gid for gid, m in meters.items() if match and m.id == match.meter_id), None)

    warnings = []
    chosen = by_id.get(garage_id) if garage_id else None
    if chosen is None:
        chosen = by_id.get(matched_garage) if matched_garage else (garages[0] if len(garages) == 1 else None)

    result = {
        "photo_id": photo.id,
        "photo_url": url_for("meter_photos.photo_file", photo_id=photo.id),
        "ocr_error": bool(photo.ocr_error),
        "found_text": " · ".join(t["text"].strip() for t in tokens[:12]) if tokens else "",
        "garage_id": chosen.id if chosen else None,
        "meter_number": None,
        "meter_status": "none",
        "baseline": None,
        "baseline_date": None,
        "options": [],
        "suggestion": None,
        # Правлению — дата съёмки (сфотографировали на обходе, вносят позже).
        "reading_date": (
            photo.taken_date if photo.taken_date and photo.taken_date <= dt.date.today() else dt.date.today()
        ).isoformat(),
        "warnings": warnings,
    }
    if photo.ocr_error:
        warnings.append(_("Распознать фото не удалось — введите показания вручную."))
    if chosen is None:
        if not photo.ocr_error:
            warnings.append(_("Номер счётчика на фото не найден среди ваших счётчиков — выберите гараж."))
        return result

    meter = meters[chosen.id]
    result["meter_number"] = meter.meter_number
    if match and match.meter_id == meter.id:
        result["meter_status"] = "matched" if match.score == 1 else "similar"
    elif matched_garage and matched_garage != chosen.id:
        result["meter_status"] = "other"
        warnings.append(_(
            "Номер на фото — счётчик гаража №{number}, а выбран гараж №{chosen}.",
            number=by_id[matched_garage].number, chosen=chosen.number,
        ))
    elif not photo.ocr_error:
        known = digits_only(meter.meter_number)
        if known and any(len(n.digits) >= 5 for n in numbers):
            result["meter_status"] = "not_found"
            warnings.append(_("Номер счётчика {number} на фото не найден — проверьте, тот ли это счётчик.", number=meter.meter_number))

    previous = latest_reading(meter)
    baseline = previous.reading if previous else meter.initial_reading
    baseline_date = previous.reading_date if previous else meter.installed_date
    result["baseline"] = str(baseline) if baseline is not None else None
    result["baseline_date"] = baseline_date.isoformat() if baseline_date else None
    days = (dt.date.today() - baseline_date).days if baseline_date else None

    options = reading_options(
        numbers, serial_digits=(match.found if match else digits_only(meter.meter_number)) or None,
        baseline=baseline, days=days, avg_daily=_avg_daily(meter),
    )
    result["options"] = [
        {
            "value": str(o.value), "text": o.text, "tenths": o.tenths_assumed,
            "warnings": [_warning_text(w) for w in o.warnings],
        }
        for o in options[:6]
    ]
    if options and not options[0].warnings:
        result["suggestion"] = str(options[0].value)
    elif not photo.ocr_error:
        warnings.append(_("Показания на фото уверенно не найдены — проверьте значение."))
    return result


def _photo_for_user(photo_id):
    photo = database.db_session.get(MeterPhoto, photo_id)
    if photo is None:
        abort(404)
    if not is_board() and photo.uploaded_by_user_id != g.user.id:
        abort(403)
    return photo


@bp.route("/")
@login_required
def index():
    garages = available_garages()
    return render_template(
        "meter_photos/index.html",
        garages=[(garage, _current_meter(garage)) for garage in garages],
        preselected_garage_id=request.args.get("garage_id", type=int),
        today=dt.date.today(),
    )


@bp.route("/recognize", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def recognize():
    garages = available_garages()
    if not garages:
        return jsonify(ok=False, error=_("У вас нет гаражей со счётчиком.")), 403
    file_storage = request.files.get("photo")
    ext = os.path.splitext(file_storage.filename or "")[1].lower() if file_storage else ""
    if not file_storage or ext not in ALLOWED_EXT:
        return jsonify(ok=False, error=_("Нужна фотография (JPEG, PNG или WebP).")), 400

    _cleanup_stale(g.user.id)
    stored = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    file_storage.save(path)
    ocr = run_ocr(path)  # по исходному снимку — мелкий номер на шильдике
    shrink_image(path)

    taken = None
    if ocr.get("exif_date"):
        try:
            taken = dt.date.fromisoformat(ocr["exif_date"])
        except ValueError:
            taken = None
    photo = MeterPhoto(
        uploaded_by_user_id=g.user.id, file_path=stored,
        ocr_tokens=json.dumps(ocr["tokens"], ensure_ascii=False) if "tokens" in ocr else None,
        ocr_error=ocr.get("error"), taken_date=taken,
    )
    database.db_session.add(photo)
    database.db_session.flush()
    if ocr.get("error"):
        current_app.logger.warning("meter photo %s: OCR failed: %s", photo.id, ocr["error"])

    result = analyze(photo, garages, request.form.get("garage_id", type=int))
    photo.garage_id = result["garage_id"]
    photo.suggested_reading = Decimal(result["suggestion"]) if result["suggestion"] else None
    photo.suggested_meter_number = (
        result["meter_number"] if result["meter_status"] in ("matched", "similar") else None
    )
    database.db_session.commit()
    return jsonify(ok=True, **result)


@bp.route("/<int:photo_id>/analyze")
@login_required
def reanalyze(photo_id):
    """Тот же разбор для другого гаража (человек сменил гараж в списке)."""
    photo = _photo_for_user(photo_id)
    return jsonify(ok=True, **analyze(photo, available_garages(), request.args.get("garage_id", type=int)))


@bp.route("/<int:photo_id>/confirm", methods=["POST"])
@login_required
def confirm(photo_id):
    photo = _photo_for_user(photo_id)
    if photo.reading_id is not None:
        return jsonify(ok=False, error=_("По этому фото показания уже внесены.")), 400
    garage = database.db_session.get(Garage, request.form.get("garage_id", type=int) or 0)
    if garage is None:
        return jsonify(ok=False, error=_("Выберите гараж.")), 400
    if not is_owner_or_board(garage):
        abort(403)
    meter = _current_meter(garage)
    if meter is None:
        return jsonify(ok=False, error=_("Сначала добавьте счётчик.")), 400
    try:
        value = parse_decimal(request.form.get("reading", ""))
    except (InvalidOperation, ValueError):
        return jsonify(ok=False, error=_("Некорректное значение показаний.")), 400
    reading_date = dt.date.today()
    if is_board() and request.form.get("reading_date"):
        try:
            reading_date = dt.date.fromisoformat(request.form["reading_date"])
        except ValueError:
            return jsonify(ok=False, error=_("Некорректная дата снятия показаний.")), 400

    try:
        reading, amount = record_electricity_reading(garage, meter, value, reading_date)
    except ReadingError as e:
        database.db_session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    photo.garage_id, photo.meter_id, photo.reading_id = garage.id, meter.id, reading.id
    database.db_session.commit()

    if amount is None:
        message = _("Показания внесены. Сумма не рассчитана — задайте тариф на странице «Электроэнергия».")
    else:
        message = _("Показания внесены, начислено {amount} ₽.", amount=amount)
    return jsonify(
        ok=True, message=_("Гараж №{number}", number=garage.number) + ": " + message,
        garage_url=url_for("garages.detail", garage_id=garage.id, tab="account"),
    )


@bp.route("/<int:photo_id>/file")
@login_required
def photo_file(photo_id):
    photo = database.db_session.get(MeterPhoto, photo_id)
    if photo is None:
        abort(404)
    allowed = (
        is_board() or photo.uploaded_by_user_id == g.user.id
        or (photo.garage is not None and is_owner_or_board(photo.garage))
    )
    if not allowed:
        abort(403)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], photo.file_path)


def _cleanup_stale(user_id):
    """Неподтверждённые фото человека старше суток — снимок без показаний
    не нужен ни как подтверждение, ни для обучения (нет верных цифр)."""
    stale = (
        database.db_session.query(MeterPhoto)
        .filter(MeterPhoto.uploaded_by_user_id == user_id, MeterPhoto.reading_id.is_(None),
                MeterPhoto.created_at < dt.datetime.now() - STALE_UNCONFIRMED)
        .all()
    )
    for photo in stale:
        try:
            os.remove(os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path))
        except OSError:
            pass
        database.db_session.delete(photo)
