"""
Схема территории кооператива для правления: контуры боксов на SVG-полотне,
клик по боксу — карточка гаража, окраска по долгу/электричеству/земле,
поиск по номеру. В режиме правки контуры двигаются мышью, привязываются к
гаражам реестра, добавляются и удаляются; сохраняется всё разом (AJAX).

Данные — GarageMapShape/GarageMapSettings (см. их докстринги). Первичная
раскладка загружается скриптом scripts/import_garage_map.py.
"""
import datetime as dt
import json
import os
from decimal import Decimal

from flask import Blueprint, render_template, request, jsonify, abort, current_app, send_from_directory
from sqlalchemy.orm import joinedload

from . import database
from .accounting import balance
from .auth import roles_required
from .i18n import translate as _
from .models import (
    Garage, GarageMapShape, GarageMapSettings, GarageOwnership, MemberAccount, RoleEnum, garage_sort_key,
)
from .uploads import save_upload

bp = Blueprint("garage_map", __name__, url_prefix="/garages/map")

SHAPE_KINDS = ("garage", "road", "building", "area")
BACKGROUND_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# Показания старше стольких дней на схеме считаются «давно не подавались».
STALE_READING_DAYS = 62


def get_settings() -> GarageMapSettings:
    settings = database.db_session.get(GarageMapSettings, 1)
    if settings is None:
        settings = GarageMapSettings(id=1, width=1000, height=1000, background_opacity=50)
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


def _current_meter(garage: Garage):
    if not garage.meters:
        return None
    return sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id))[-1]


def _garage_states(garages: list[Garage]) -> dict[int, dict]:
    """Всё, чем красится бокс на схеме: долг (электричество + взносы),
    счётчик и давность показаний, земля, территория."""
    dues: dict[int, Decimal] = {}
    accounts = (
        database.db_session.query(MemberAccount)
        .filter(MemberAccount.garage_id.isnot(None), MemberAccount.is_archived.is_(False))
        .options(joinedload(MemberAccount.charges), joinedload(MemberAccount.payments))
        .all()
    )
    for account in accounts:
        dues[account.garage_id] = dues.get(account.garage_id, Decimal("0")) + balance(account)

    today = dt.date.today()
    states = {}
    for garage in garages:
        power = balance(garage)
        fees = dues.get(garage.id, Decimal("0"))
        meter = _current_meter(garage)
        last_reading = None
        if meter is not None and meter.readings:
            last_reading = max(r.reading_date for r in meter.readings)
        if meter is None:
            reading_state = "nometer"
        elif last_reading is None or (today - last_reading).days > STALE_READING_DAYS:
            reading_state = "stale"
        else:
            reading_state = "fresh"
        states[garage.id] = {
            "debt": "debt" if power + fees < 0 else "ok",
            "power": reading_state,
            "land": "privatized" if garage.land_privatized else "notprivatized",
            "territory": "outside" if garage.outside_territory else "inside",
            "owners": ", ".join(o.person.full_name for o in garage.ownerships),
            "total": power + fees,
        }
    return states


def _shape_dict(shape: GarageMapShape) -> dict:
    return {
        "id": shape.id, "kind": shape.kind, "garage_id": shape.garage_id,
        "label": shape.label or "", "points": shape.point_list(),
    }


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    settings = get_settings()
    shapes = (
        database.db_session.query(GarageMapShape)
        .options(joinedload(GarageMapShape.garage))
        .order_by(GarageMapShape.sort_order, GarageMapShape.id)
        .all()
    )
    garages = sorted(
        database.db_session.query(Garage)
        .options(
            joinedload(Garage.meters), joinedload(Garage.charges), joinedload(Garage.payments),
            joinedload(Garage.ownerships).joinedload(GarageOwnership.person),
        )
        .all(),
        key=garage_sort_key,
    )
    states = _garage_states(garages)
    placed = {s.garage_id for s in shapes if s.garage_id}
    unplaced = [g for g in garages if g.id not in placed]
    database.db_session.commit()  # get_settings мог завести строку настроек
    return render_template(
        "garage_map/view.html",
        settings=settings, shapes=shapes, states=states, garages=garages, unplaced=unplaced,
        shapes_json=json.dumps([_shape_dict(s) for s in shapes], ensure_ascii=False),
        garages_json=json.dumps([{"id": g.id, "number": g.number} for g in garages], ensure_ascii=False),
        states_json=json.dumps(
            {g_id: {k: v for k, v in st.items() if k != "total"} for g_id, st in states.items()}, ensure_ascii=False,
        ),
    )


@bp.route("/card/<int:garage_id>")
@roles_required(RoleEnum.BOARD)
def card(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    state = _garage_states([garage])[garage.id]
    meter = _current_meter(garage)
    last_reading = None
    if meter is not None and meter.readings:
        last_reading = max(meter.readings, key=lambda r: (r.reading_date, r.id))
    fees = [
        a for a in database.db_session.query(MemberAccount)
        .filter(MemberAccount.garage_id == garage.id, MemberAccount.is_archived.is_(False)).all()
    ]
    fee_rows = [(a, balance(a)) for a in sorted(fees, key=lambda a: (a.fee_type.name, a.person.full_name))]
    return render_template(
        "garage_map/_card.html", garage=garage, state=state, power_balance=balance(garage),
        fee_rows=fee_rows, meter=meter, last_reading=last_reading,
        photos=sorted(garage.photos, key=lambda p: p.id, reverse=True),
    )


def _clean_points(raw) -> list[list[float]] | None:
    if not isinstance(raw, list) or len(raw) < 3 or len(raw) > 200:
        return None
    points = []
    for point in raw:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        points.append([round(x, 1), round(y, 1)])
    return points


@bp.route("/save", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def save():
    """Сохраняет схему целиком: присланный список фигур заменяет текущий
    (фигуры с id обновляются, без id — создаются, отсутствующие — удаляются)."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("shapes")
    if not isinstance(items, list):
        return jsonify(ok=False, error=_("Нет данных схемы.")), 400

    existing = {s.id: s for s in database.db_session.query(GarageMapShape).all()}
    valid_garage_ids = {gid for (gid,) in database.db_session.query(Garage.id).all()}
    seen_garages: set[int] = set()
    keep_ids: set[int] = set()
    prepared = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return jsonify(ok=False, error=_("Неверные данные схемы.")), 400
        points = _clean_points(item.get("points"))
        if points is None:
            return jsonify(ok=False, error=_("У фигуры неверный контур.")), 400
        kind = item.get("kind") if item.get("kind") in SHAPE_KINDS else "garage"
        garage_id = item.get("garage_id")
        if garage_id in ("", None) or kind != "garage":
            garage_id = None
        else:
            try:
                garage_id = int(garage_id)
            except (TypeError, ValueError):
                return jsonify(ok=False, error=_("Неверный гараж.")), 400
            if garage_id not in valid_garage_ids:
                return jsonify(ok=False, error=_("Гараж не найден.")), 400
            if garage_id in seen_garages:
                number = database.db_session.get(Garage, garage_id).number
                return jsonify(ok=False, error=_("Гараж №{number} привязан к двум контурам.", number=number)), 400
            seen_garages.add(garage_id)
        label = (str(item.get("label") or "").strip() or None)
        if label:
            label = label[:100]
        shape_id = item.get("id")
        shape = existing.get(shape_id) if isinstance(shape_id, int) else None
        if shape is not None:
            keep_ids.add(shape.id)
        prepared.append((shape, kind, garage_id, label, points, index))

    # Сначала удаляем и отвязываем — иначе уникальный garage_id споткнётся,
    # когда гараж переносят с одного контура на другой.
    for shape_id, shape in existing.items():
        if shape_id not in keep_ids:
            database.db_session.delete(shape)
        else:
            shape.garage_id = None
    database.db_session.flush()

    for shape, kind, garage_id, label, points, index in prepared:
        if shape is None:
            shape = GarageMapShape()
            database.db_session.add(shape)
        shape.kind = kind
        shape.garage_id = garage_id
        shape.label = label
        shape.points = json.dumps(points)
        shape.sort_order = index

    settings = get_settings()
    opacity = payload.get("background_opacity")
    if opacity is not None:
        try:
            settings.background_opacity = max(0, min(100, int(opacity)))
        except (TypeError, ValueError):
            pass
    database.db_session.commit()
    return jsonify(ok=True)


@bp.route("/background", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def upload_background():
    upload = request.files.get("file")
    if not upload or os.path.splitext(upload.filename or "")[1].lower() not in BACKGROUND_EXT:
        return jsonify(ok=False, error=_("Нужен снимок в формате jpg, png или webp.")), 400
    stored = save_upload(upload, current_app.config["UPLOAD_FOLDER"], allowed_ext=BACKGROUND_EXT)
    settings = get_settings()
    old = settings.background_file
    settings.background_file = stored
    database.db_session.commit()
    if old:
        try:
            os.remove(os.path.join(current_app.config["UPLOAD_FOLDER"], old))
        except OSError:
            pass
    return jsonify(ok=True)


@bp.route("/background/file")
@roles_required(RoleEnum.BOARD)
def background_file():
    settings = database.db_session.get(GarageMapSettings, 1)
    if settings is None or not settings.background_file:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], settings.background_file)
