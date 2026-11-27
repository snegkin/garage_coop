"""
Схема территории кооператива для правления: контуры боксов на SVG-полотне,
клик по боксу — карточка гаража, окраска по долгу/электричеству/земле,
поиск по номеру. В режиме правки контуры двигаются мышью, привязываются к
гаражам реестра, добавляются и удаляются, углы добавляются и убираются;
сохраняется всё разом (AJAX).

Объекты (GarageMapObject) — значки в точке: электрощит, фонарь, счётчик
(со ссылкой на главный счётчик кооператива или контрольный счётчик ряда),
рубильник, отметка, Wi-Fi (роутер/хотспот/мост/репитер с моделью, IP,
MAC, подключением к другому устройству — по нему рисуется топология сети —
ссылкой на вики и файлами бэкапа настроек); у каждого описание и фото. Положение
и свойства сохраняются вместе со схемой, фото — сразу (у сохранённого объекта).

Данные — GarageMapShape/GarageMapSettings/GarageMapObject (см. их докстринги). Первичная
раскладка загружается скриптом scripts/import_garage_map.py.
"""
import datetime as dt
import ipaddress
import json
import os
import re
from decimal import Decimal

from flask import Blueprint, render_template, request, jsonify, abort, current_app, send_from_directory
from sqlalchemy.orm import joinedload

from . import database
from .accounting import balance
from .auth import roles_required
from .i18n import translate as _
from .models import (
    ControlMeter, Garage, GarageMapObject, PowerPhaseDevice, PowerPhaseReading, GarageMapObjectFile, GarageMapObjectPhoto, GarageMapShape,
    GarageMapSettings, GarageOwnership, MasterMeterReading, MemberAccount, RoleEnum, WikiPage, garage_sort_key,
)
from .uploads import WIKI_ATTACHMENT_ALLOWED_EXT, save_upload

bp = Blueprint("garage_map", __name__, url_prefix="/garages/map")

SHAPE_KINDS = ("garage", "road", "building", "area")
OBJECT_KINDS = ("panel", "lamp", "meter", "switch", "wifi", "mark")
METER_KINDS = ("panel", "meter", "switch")  # у них бывает привязка к счётчику в учёте
NET_KINDS = ("panel", "meter", "switch", "wifi")  # у них бывают IP/MAC и подключение к сети
# Назначение Wi-Fi-устройства: роутер, раздача Wi-Fi, Wi-Fi мост, репитер (подписи — в шаблонах).
NET_ROLES = ("router", "hotspot", "bridge", "repeater")
# Бэкапы настроек: форматы как у вложений вики плюс бинарные бэкапы прошивок.
BACKUP_EXT = WIKI_ATTACHMENT_ALLOWED_EXT | {".bin", ".bak", ".backup", ".dat"}
MAC_RE = re.compile(r"^[0-9A-F]{2}([:-]?[0-9A-F]{2}){5}$")
BACKGROUND_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
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


def _object_dict(obj: GarageMapObject) -> dict:
    return {
        "id": obj.id, "kind": obj.kind, "x": obj.x, "y": obj.y, "title": obj.title or "",
        "description": obj.description or "", "master": bool(obj.master),
        "control_meter_id": obj.control_meter_id, "photos": len(obj.photos),
        "model": obj.model or "", "hostname": obj.hostname or "", "ip_address": obj.ip_address or "",
        "mac": obj.mac or "", "net_role": obj.net_role or "", "uplink_id": obj.uplink_id,
        "wiki_page_id": obj.wiki_page_id, "power_device_id": obj.power_device_id,
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
    objects = (
        database.db_session.query(GarageMapObject)
        .options(joinedload(GarageMapObject.photos))
        .order_by(GarageMapObject.id)
        .all()
    )
    control_meters = database.db_session.query(ControlMeter).order_by(ControlMeter.name).all()
    wiki_pages = database.db_session.query(WikiPage.id, WikiPage.title).order_by(WikiPage.title).all()
    power_devices = database.db_session.query(PowerPhaseDevice).order_by(PowerPhaseDevice.sort_order, PowerPhaseDevice.id).all()
    placed = {s.garage_id for s in shapes if s.garage_id}
    unplaced = [g for g in garages if g.id not in placed]
    database.db_session.commit()  # get_settings мог завести строку настроек
    return render_template(
        "garage_map/view.html",
        settings=settings, shapes=shapes, states=states, garages=garages, unplaced=unplaced,
        shapes_json=json.dumps([_shape_dict(s) for s in shapes], ensure_ascii=False),
        garages_json=json.dumps([{"id": g.id, "number": g.number} for g in garages], ensure_ascii=False),
        objects_json=json.dumps([_object_dict(o) for o in objects], ensure_ascii=False),
        meters_json=json.dumps([{"id": m.id, "name": m.name} for m in control_meters], ensure_ascii=False),
        wiki_json=json.dumps([{"id": pid, "title": title} for pid, title in wiki_pages], ensure_ascii=False),
        power_json=json.dumps([{"id": d.id, "name": d.label} for d in power_devices], ensure_ascii=False),
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


def _get_object(object_id: int) -> GarageMapObject:
    obj = database.db_session.get(GarageMapObject, object_id)
    if obj is None:
        abort(404)
    return obj


@bp.route("/object/<int:object_id>")
@roles_required(RoleEnum.BOARD)
def object_card(object_id):
    obj = _get_object(object_id)
    last_master = None
    if obj.master:
        last_master = (
            database.db_session.query(MasterMeterReading)
            .order_by(MasterMeterReading.reading_date.desc(), MasterMeterReading.id.desc())
            .first()
        )
    last_control = None
    if not obj.master and obj.control_meter is not None and obj.control_meter.readings:
        last_control = max(obj.control_meter.readings, key=lambda r: (r.reading_date, r.id))
    last_power = None
    if obj.power_device is not None:
        last_power = (
            database.db_session.query(PowerPhaseReading)
            .filter(PowerPhaseReading.device_id == obj.power_device_id)
            .order_by(PowerPhaseReading.ts.desc())
            .first()
        )
    return render_template(
        "garage_map/_object_card.html", obj=obj, last_master=last_master, last_control=last_control,
        last_power=last_power,
        # показания POWCT пишутся в UTC — в карточке местное время сервера
        power_at=last_power.ts.replace(tzinfo=dt.timezone.utc).astimezone() if last_power else None,
    )


@bp.route("/object/<int:object_id>/photos", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_add_photos(object_id):
    obj = _get_object(object_id)
    uploads = [f for f in request.files.getlist("file") if f and f.filename]
    if not uploads:
        return jsonify(ok=False, error=_("Выберите фотографию.")), 400
    for upload in uploads:
        if os.path.splitext(upload.filename)[1].lower() not in PHOTO_EXT:
            return jsonify(ok=False, error=_("Поддерживаются только изображения (jpg, png, webp, gif).")), 400
    caption = (request.form.get("caption") or "").strip()[:255] or None
    for upload in uploads:
        stored = save_upload(upload, current_app.config["UPLOAD_FOLDER"], allowed_ext=PHOTO_EXT)
        database.db_session.add(GarageMapObjectPhoto(object_id=obj.id, file_path=stored, caption=caption))
    database.db_session.commit()
    return jsonify(ok=True)


@bp.route("/object/photos/<int:photo_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_remove_photo(photo_id):
    photo = database.db_session.get(GarageMapObjectPhoto, photo_id)
    if photo is None:
        abort(404)
    path = photo.file_path
    database.db_session.delete(photo)
    database.db_session.commit()
    _remove_upload(path)
    return jsonify(ok=True)


@bp.route("/object/photos/<int:photo_id>/file")
@roles_required(RoleEnum.BOARD)
def object_photo_file(photo_id):
    photo = database.db_session.get(GarageMapObjectPhoto, photo_id)
    if photo is None:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], photo.file_path)


@bp.route("/object/<int:object_id>/files", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_add_files(object_id):
    obj = _get_object(object_id)
    uploads = [f for f in request.files.getlist("file") if f and f.filename]
    if not uploads:
        return jsonify(ok=False, error=_("Выберите файл.")), 400
    for upload in uploads:
        if os.path.splitext(upload.filename)[1].lower() not in BACKUP_EXT:
            return jsonify(ok=False, error=_("Такой тип файла не поддерживается: {name}", name=upload.filename)), 400
    for upload in uploads:
        stored = save_upload(upload, current_app.config["UPLOAD_FOLDER"], allowed_ext=BACKUP_EXT)
        database.db_session.add(GarageMapObjectFile(
            object_id=obj.id, file_path=stored, original_name=os.path.basename(upload.filename)[:255],
        ))
    database.db_session.commit()
    return jsonify(ok=True)


@bp.route("/object/files/<int:file_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_remove_file(file_id):
    item = database.db_session.get(GarageMapObjectFile, file_id)
    if item is None:
        abort(404)
    path = item.file_path
    database.db_session.delete(item)
    database.db_session.commit()
    _remove_upload(path)
    return jsonify(ok=True)


@bp.route("/object/files/<int:file_id>/download")
@roles_required(RoleEnum.BOARD)
def object_file_download(file_id):
    item = database.db_session.get(GarageMapObjectFile, file_id)
    if item is None:
        abort(404)
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"], item.file_path, as_attachment=True, download_name=item.original_name,
    )


def _remove_upload(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(os.path.join(current_app.config["UPLOAD_FOLDER"], path))
    except OSError:
        pass


def _clean_ip(raw) -> tuple[str | None, bool]:
    """«192.168.2.151/24» или «192.168.2.151/255.255.255.0» → «192.168.2.151/24»;
    без маски — просто адрес. Возвращает (значение, ok)."""
    value = str(raw or "").strip()
    if not value:
        return None, True
    try:
        if "/" in value:
            return ipaddress.ip_interface(value).with_prefixlen, True
        return str(ipaddress.ip_address(value)), True
    except ValueError:
        return None, False


def _clean_mac(raw) -> tuple[str | None, bool]:
    value = str(raw or "").strip().upper()
    if not value:
        return None, True
    if not MAC_RE.match(value):
        return None, False
    digits = re.sub(r"[^0-9A-F]", "", value)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)), True


def _clean_objects(items, valid_meter_ids: set[int], valid_wiki_ids: set[int], valid_power_ids: set[int]):
    """Проверяет присланные объекты; возвращает (список, ошибка).
    Подключение (uplink) приходит индексом в этом же списке — так можно
    связать и только что добавленные, ещё без id, устройства."""
    if not isinstance(items, list) or len(items) > 2000:
        return None, _("Неверные данные объектов схемы.")
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            return None, _("Неверные данные объектов схемы.")
        try:
            x, y = round(float(item.get("x")), 1), round(float(item.get("y")), 1)
        except (TypeError, ValueError):
            return None, _("У объекта неверное положение.")
        kind = item.get("kind") if item.get("kind") in OBJECT_KINDS else "mark"
        master = bool(item.get("master")) and kind in METER_KINDS
        meter_id = item.get("control_meter_id")
        if master or kind not in METER_KINDS or meter_id in ("", None):
            meter_id = None
        else:
            try:
                meter_id = int(meter_id)
            except (TypeError, ValueError):
                return None, _("Неверный контрольный счётчик.")
            if meter_id not in valid_meter_ids:
                return None, _("Контрольный счётчик не найден.")
        net = {"model": None, "hostname": None, "ip_address": None, "mac": None, "net_role": None,
               "uplink_index": None, "wiki_page_id": None, "power_device_id": None}
        if kind in NET_KINDS:
            title_hint = str(item.get("title") or "").strip() or str(item.get("ip_address") or "")
            ip, ok = _clean_ip(item.get("ip_address"))
            if not ok:
                return None, _("Неверный IP-адрес у «{name}»: укажите вида 192.168.2.151/24.", name=title_hint)
            mac, ok = _clean_mac(item.get("mac"))
            if not ok:
                return None, _("Неверный MAC-адрес у «{name}».", name=title_hint)
            uplink_index = item.get("uplink_index")
            if uplink_index is not None:
                if not isinstance(uplink_index, int) or not 0 <= uplink_index < len(items) \
                        or uplink_index == len(cleaned):
                    return None, _("Неверное подключение у «{name}».", name=title_hint)
            wiki_id = item.get("wiki_page_id")
            if wiki_id in ("", None):
                wiki_id = None
            else:
                try:
                    wiki_id = int(wiki_id)
                except (TypeError, ValueError):
                    wiki_id = -1
                if wiki_id not in valid_wiki_ids:
                    return None, _("Страница вики не найдена.")
            power_id = item.get("power_device_id") if kind == "meter" else None
            if power_id not in ("", None):
                try:
                    power_id = int(power_id)
                except (TypeError, ValueError):
                    power_id = -1
                if power_id not in valid_power_ids:
                    return None, _("Устройство мониторинга не найдено.")
            else:
                power_id = None
            net = {
                "model": str(item.get("model") or "").strip()[:200] or None,
                "hostname": str(item.get("hostname") or "").strip()[:100] or None,
                "ip_address": ip, "mac": mac,
                "net_role": item.get("net_role") if kind == "wifi" and item.get("net_role") in NET_ROLES else None,
                "uplink_index": uplink_index, "wiki_page_id": wiki_id, "power_device_id": power_id,
            }
        cleaned.append({
            "id": item.get("id") if isinstance(item.get("id"), int) else None,
            "kind": kind, "x": x, "y": y,
            "title": str(item.get("title") or "").strip()[:200] or None,
            "description": str(item.get("description") or "").strip()[:5000] or None,
            "master": master, "control_meter_id": meter_id, **net,
        })
    # подключение — только к Wi-Fi-устройству и без петель
    for index, data in enumerate(cleaned):
        seen = {index}
        current = data["uplink_index"]
        while current is not None:
            if cleaned[current]["kind"] != "wifi":
                return None, _("Подключать можно только к Wi-Fi-устройству.")
            if current in seen:
                return None, _("Подключения устройств замкнуты в кольцо.")
            seen.add(current)
            current = cleaned[current]["uplink_index"]
    return cleaned, None


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

    removed_files: list[str] = []
    if "objects" in payload:
        valid_meter_ids = {mid for (mid,) in database.db_session.query(ControlMeter.id).all()}
        valid_wiki_ids = {pid for (pid,) in database.db_session.query(WikiPage.id).all()}
        valid_power_ids = {did for (did,) in database.db_session.query(PowerPhaseDevice.id).all()}
        objects, error = _clean_objects(payload.get("objects"), valid_meter_ids, valid_wiki_ids, valid_power_ids)
        if error:
            database.db_session.rollback()
            return jsonify(ok=False, error=error), 400
        existing_objects = {o.id: o for o in database.db_session.query(GarageMapObject).all()}
        keep_objects = {o["id"] for o in objects if o["id"] in existing_objects}
        for obj_id, obj in existing_objects.items():
            if obj_id not in keep_objects:
                removed_files.extend(p.file_path for p in obj.photos)
                removed_files.extend(f.file_path for f in obj.files)
                for child in obj.downlinks:
                    child.uplink_id = None
                database.db_session.delete(obj)
        database.db_session.flush()
        saved = []
        for data in objects:
            obj = existing_objects.get(data["id"]) if data["id"] in keep_objects else None
            if obj is None:
                obj = GarageMapObject()
                database.db_session.add(obj)
            for field in ("kind", "x", "y", "title", "description", "master", "control_meter_id",
                          "model", "hostname", "ip_address", "mac", "net_role", "wiki_page_id", "power_device_id"):
                setattr(obj, field, data[field])
            saved.append(obj)
        database.db_session.flush()
        for obj, data in zip(saved, objects):
            obj.uplink_id = saved[data["uplink_index"]].id if data["uplink_index"] is not None else None

    settings = get_settings()
    opacity = payload.get("background_opacity")
    if opacity is not None:
        try:
            settings.background_opacity = max(0, min(100, int(opacity)))
        except (TypeError, ValueError):
            pass
    database.db_session.commit()
    for path in removed_files:
        _remove_upload(path)
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
