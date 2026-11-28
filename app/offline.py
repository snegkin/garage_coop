"""
Оффлайн-режим для правления: на территории ГСК мобильный интернет работает
только по белым спискам, сайт оттуда недоступен. Телефон председателя или
члена правления заранее (дома, по Wi-Fi) скачивает слепок базы, нужный в
поле, и хранит его в IndexedDB; оффлайн-страница (/offline/) показывает его
без сети.

Слепок — только то, что нужно на территории: гаражи с собственниками и
контактами, телефоны, долги (электричество по гаражу, взносы по лицевым
счетам членов), текущий счётчик с последним показанием, контрольные
счётчики. Без паспортов, адресов и прочего, что в поле не нужно. Плюс
справочное: схема территории (контуры, линии, оборудование с описаниями,
фото и сетевыми адресами — без логинов и паролей устройств) и вики
(страницы готовым HTML, подстановки живых данных — на момент слепка;
из вложений на телефон — только картинки и PDF/TXT, не архивы и бэкапы
настроек устройств).

Картинки (подложка схемы, фото оборудования, картинки и вложения вики)
слепок перечисляет в media — store.js скачивает их в отдельный кеш
(MEDIA_CACHE), service worker отдаёт их оттуда; у каждой картинки свой
неизменный адрес (у подложки — с ?v= от имени файла).

Обновляет слепок сам сайт: base.html для правления подключает
static/offline/store.js — при открытии страницы, не чаще раза в
CHECK_INTERVAL, запрос с If-None-Match; сервер отвечает 304, если ничего не
поменялось. Оффлайн-страница — статическая оболочка без данных (данные
только из IndexedDB), её и статику кеширует service worker (main.py), он же
подменяет ею страницу сайта, когда сеть недоступна.

Выход из учётной записи стирает слепок на устройстве (auth.logout ставит
флаг offline_wipe, base.html удаляет базу), ответ 401/403 на обновление —
тоже (роль сняли, вошёл другой человек без прав правления).
"""
import datetime as dt
import hashlib
import json
import os
from decimal import Decimal

import re

from flask import Blueprint, Response, g, render_template, request, url_for
from sqlalchemy import func

from . import database
from .models import (
    Charge, ControlMeter, ControlMeterReading, Cooperative, ElectricityMeter, ElectricityReading,
    FeeType, Garage, GarageContact, GarageMapObject, GarageMapShape, GarageOwnership, MasterMeterReading,
    MemberAccount, Payment, Person, WikiPage,
)
from .offline_sync import last_receipt_seq
from .permissions import is_board, is_privileged

bp = Blueprint("offline", __name__, url_prefix="/offline")

# Версия формата слепка: оффлайн-страница не показывает слепок другой
# версии (после обновления сайта он перекачается при первой же проверке).
SNAPSHOT_FORMAT = 3


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _reading(value) -> str | None:
    """Показание без хвостовых нулей: 12345.00 → "12345", 12345.50 → "12345.5"."""
    if value is None:
        return None
    return f"{Decimal(value).normalize():f}"


def _sums(column, key_column) -> dict:
    return dict(
        database.db_session.query(key_column, func.sum(column))
        .filter(key_column.isnot(None))
        .group_by(key_column)
        .all()
    )


def _last_readings(meter_column, date_column, value_column, id_column) -> dict:
    """{meter_id: (показание, дата)} — последнее по дате снятия (при равных
    датах — по id), одним запросом по трём столбцам, без загрузки моделей."""
    last = {}
    rows = (
        database.db_session.query(meter_column, value_column, date_column)
        .order_by(meter_column, date_column, id_column)
        .all()
    )
    for meter_id, value, date in rows:
        last[meter_id] = (value, date)
    return last


def build_snapshot() -> dict:
    """Данные слепка (без generated_at — по ним считается ETag)."""
    session = database.db_session
    coop = session.query(Cooperative).first()
    with_penalty = is_privileged()  # строки пени рядовому члену правления не показываем (как на сайте)

    fee_types = session.query(FeeType).order_by(FeeType.id).all()
    fee_types = [f for f in fee_types if with_penalty or not f.is_penalty]
    fee_type_ids = {f.id for f in fee_types}

    # Электричество — общий счёт гаража: balance(garage) = оплаты − начисления.
    garage_charged = _sums(Charge.amount, Charge.garage_id)
    garage_paid = _sums(Payment.amount, Payment.garage_id)

    # Взносы — лицевые счета членов (только активные).
    account_charged = _sums(Charge.amount, Charge.account_id)
    account_paid = _sums(Payment.amount, Payment.account_id)
    accounts = []
    for ma in session.query(MemberAccount).filter(MemberAccount.is_archived.is_(False)).order_by(MemberAccount.id):
        if ma.fee_type_id not in fee_type_ids:
            continue
        bal = Decimal(account_paid.get(ma.id) or 0) - Decimal(account_charged.get(ma.id) or 0)
        # Нулевые — тоже: на них можно принять оплату (взнос вперёд).
        accounts.append({
            "id": ma.id, "person_id": ma.person_id, "garage_id": ma.garage_id, "fee_type_id": ma.fee_type_id,
            "number": ma.account_number, "balance": _money(bal),
        })

    meter_last = _last_readings(
        ElectricityReading.meter_id, ElectricityReading.reading_date,
        ElectricityReading.reading, ElectricityReading.id,
    )
    current_meter = {}
    for m in session.query(ElectricityMeter).order_by(ElectricityMeter.garage_id):
        known = current_meter.get(m.garage_id)
        if known is None or (m.installed_date or dt.date.min, m.id) > (known.installed_date or dt.date.min, known.id):
            current_meter[m.garage_id] = m

    ownerships, contacts = {}, {}
    for o in session.query(GarageOwnership).order_by(GarageOwnership.id):
        ownerships.setdefault(o.garage_id, []).append({"person_id": o.person_id, "share": str(o.share.normalize())})
    for c in session.query(GarageContact).order_by(GarageContact.id):
        contacts.setdefault(c.garage_id, []).append(
            {"person_id": c.person_id, "relation": c.relation, "tenant": bool(c.is_tenant)}
        )

    garages = []
    for garage in session.query(Garage).order_by(Garage.id):
        meter = current_meter.get(garage.id)
        last = meter_last.get(meter.id) if meter else None
        has_account = garage.id in garage_charged or garage.id in garage_paid
        garages.append({
            "id": garage.id,
            "number": garage.number,
            "outside": bool(garage.outside_territory),
            "land_privatized": bool(garage.land_privatized),
            "comment": garage.comment,
            "owners": ownerships.get(garage.id, []),
            "contacts": contacts.get(garage.id, []),
            "electricity_balance": _money(
                Decimal(garage_paid.get(garage.id) or 0) - Decimal(garage_charged.get(garage.id) or 0)
            ) if has_account else None,
            "meter": {
                "number": meter.meter_number,
                "last_reading": _reading(last[0]) if last else None,
                "last_date": last[1].isoformat() if last else None,
            } if meter else None,
        })

    # Люди — только те, кто связан с гаражом или у кого есть ненулевой счёт.
    person_ids = {o["person_id"] for items in ownerships.values() for o in items}
    person_ids |= {c["person_id"] for items in contacts.values() for c in items}
    person_ids |= {a["person_id"] for a in accounts}
    persons = []
    if person_ids:
        for p in session.query(Person).filter(Person.id.in_(person_ids)).order_by(Person.full_name, Person.id):
            persons.append({
                "id": p.id,
                "name": p.full_name,
                "archived": bool(p.is_archived),
                "phones": [{"number": ph.number, "label": ph.label} for ph in p.phones],
                "email": p.email,
                "telegram": p.telegram,
                "max": p.max_messenger,
                "comment": p.comment,
            })

    control_last = _last_readings(
        ControlMeterReading.control_meter_id, ControlMeterReading.reading_date,
        ControlMeterReading.reading, ControlMeterReading.id,
    )
    garage_numbers = {g_["id"]: g_["number"] for g_ in garages}
    control_meters = []
    for cm in session.query(ControlMeter).order_by(ControlMeter.name, ControlMeter.id):
        last = control_last.get(cm.id)
        control_meters.append({
            "id": cm.id,
            "name": cm.name,
            "parent_id": cm.parent_id,
            "parent_garage": garage_numbers.get(cm.parent_garage_id),
            "comment": cm.comment,
            "last_reading": _reading(last[0]) if last else None,
            "last_date": last[1].isoformat() if last else None,
        })

    media: list[str] = []
    garage_map = _map_snapshot(media)
    wiki = _wiki_snapshot(media)

    return {
        "format": SNAPSHOT_FORMAT,
        "coop": {"name": (coop.short_name or coop.full_name) if coop else None},
        "user": {
            "id": g.user.id, "username": g.user.username,
            # оплату председатель и бухгалтер проводят сразу, остальные — к подтверждению
            "can_post_payments": is_privileged(),
            "receipt_seq": last_receipt_seq(g.user.id),
        },
        "fee_types": [{"id": f.id, "name": f.name, "penalty": bool(f.is_penalty)} for f in fee_types],
        "garages": garages,
        "persons": persons,
        "accounts": accounts,
        "control_meters": control_meters,
        "map": garage_map,
        "wiki": wiki,
        "media": list(dict.fromkeys(media)),
    }


def _map_snapshot(media: list[str]) -> dict | None:
    """Схема территории: то же, что рисует garage_map.view, без режима
    правки. Пароли и логины устройств (device_*) не попадают никогда."""
    from .garage_map import get_settings

    session = database.db_session
    shapes = session.query(GarageMapShape).order_by(GarageMapShape.sort_order, GarageMapShape.id).all()
    objects = session.query(GarageMapObject).order_by(GarageMapObject.id).all()
    if not shapes and not objects:
        return None
    settings = get_settings()
    background = None
    if settings.background_file:
        version = hashlib.sha256(settings.background_file.encode()).hexdigest()[:12]
        background = url_for("garage_map.background_file", v=version)
        media.append(background)
    master = None
    if any(o.master for o in objects):
        last = (
            session.query(MasterMeterReading)
            .order_by(MasterMeterReading.reading_date.desc(), MasterMeterReading.id.desc())
            .first()
        )
        if last is not None:
            master = {"reading": _reading(last.reading), "date": last.reading_date.isoformat()}
    items = []
    for o in objects:
        photos = []
        for ph in sorted(o.photos, key=lambda x: x.id):
            url = url_for("garage_map.object_photo_file", photo_id=ph.id)
            media.append(url)
            photos.append({"url": url, "caption": ph.caption})
        items.append({
            "id": o.id, "kind": o.kind, "x": o.x, "y": o.y, "title": o.title, "description": o.description,
            "master": bool(o.master), "control_meter_id": o.control_meter_id, "wiki_page_id": o.wiki_page_id,
            "model": o.model, "hostname": o.hostname, "ip_address": o.ip_address, "mac": o.mac,
            "net_role": o.net_role, "uplink_id": o.uplink_id, "photos": photos,
        })
    return {
        "width": settings.width, "height": settings.height,
        "background": background, "background_opacity": settings.background_opacity,
        "shapes": [
            {"kind": sh.kind, "garage_id": sh.garage_id, "label": sh.label, "points": sh.point_list(),
             "phases": sh.phases}
            for sh in shapes
        ],
        "objects": items,
        "master_meter": master,
    }


# Картинки в тексте страницы (src=) — на телефон всегда; файлы-вложения —
# только документы для чтения: архивы и бэкапы настроек устройств (там
# пароли) на телефон не попадают, открываются только с сетью.
_WIKI_ATTACHMENT_SRC = re.compile(r'src="(/wiki/attachments/[^"]+)"')
OFFLINE_FILE_EXT = {".pdf", ".txt"}


def _wiki_snapshot(media: list[str]) -> list[dict]:
    """Страницы вики готовым HTML (тот же рендер, что на сайте, без
    сворачивания длинных листингов — на оффлайн-странице нет его скрипта).
    Внутренние страницы — тоже: слепок только для правления."""
    from . import news_format, wiki_vars

    ctx = wiki_vars.Context()
    pages = []
    for page in database.db_session.query(WikiPage).order_by(WikiPage.title, WikiPage.id):
        html = str(news_format.render_html(wiki_vars.substitute(page.body or "", ctx)))
        media.extend(m.replace("&amp;", "&") for m in _WIKI_ATTACHMENT_SRC.findall(html))
        images, files = [], []
        for att in page.attachments:
            if att.is_inline:
                continue
            url = url_for("wiki.attachment", attachment_id=att.id, original_filename=att.original_filename)
            if att.is_image:
                media.append(url)
                images.append({"url": url, "name": att.original_filename})
            else:
                offline = os.path.splitext(att.original_filename)[1].lower() in OFFLINE_FILE_EXT
                if offline:
                    media.append(url)
                files.append({"url": url, "name": att.original_filename, "offline": offline})
        updated = page.updated_at or page.created_at
        pages.append({
            "id": page.id, "title": page.title, "slug": page.slug, "parent_id": page.parent_id,
            "internal": bool(page.is_internal), "updated": updated.date().isoformat() if updated else None,
            "html": html, "images": images, "files": files,
        })
    return pages


def _json_error(status: int, message: str) -> Response:
    resp = Response(json.dumps({"error": message}, ensure_ascii=False), status=status, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/snapshot.json")
def snapshot():
    """Слепок для оффлайн-режима. JSON-ответы и при отказе (а не редирект на
    вход, как у обычных страниц) — запрашивает его скрипт, не человек."""
    if g.user is None:
        return _json_error(401, "login required")
    if not is_board():
        return _json_error(403, "board only")
    data = build_snapshot()
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
    if request.if_none_match.contains(etag):
        resp = Response(status=304)
    else:
        resp = Response(body, mimetype="application/json")
    resp.set_etag(etag)
    # Персональные данные: не в общих кешах и не в HTTP-кеше браузера —
    # храним их только в IndexedDB, откуда их стирает выход из системы.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/")
def app_shell():
    """Оффлайн-страница — оболочка без данных (всё из IndexedDB), поэтому
    открыта без входа: её заранее кеширует service worker, в том числе
    когда сессии уже нет."""
    return render_template("offline/app.html")
