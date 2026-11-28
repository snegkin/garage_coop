"""
Записи правления, сделанные без сети (оффлайн-страница /offline/, см.
app/offline.py), — приём с телефона и внесение в учёт.

Телефон копит записи в IndexedDB и, как только сайт снова доступен,
отправляет их пачкой (POST /offline/sync) в том порядке, в каком их
делали. Каждая запись — FieldEntry с uuid от телефона: повтор той же
записи (связь оборвалась на середине) возвращает её прежний итог, а не
вносит второй раз.

Внесение — теми же функциями, что и формы сайта, с теми же проверками:
  reading          показания счётчика гаража (garages.record_electricity_reading;
                   дата — дата снятия на телефоне, по ней и тариф) + фото
  control_reading  показания контрольного счётчика
  meter            новый (заменённый) счётчик гаража
  payment          принятая оплата: электричество гаража или лицевой счёт.
                   Председатель и бухгалтер проводят её сразу, рядовой член
                   правления — «ждёт подтверждения» (на сайте платежи
                   вносят только они), по номеру квитанции
  person_create    новый человек (+ сразу собственник, совладелец или
                   лицо для связи гаража)
  person_edit      правка карточки: телефон присылает и старые значения
                   полей — если на сайте поле успели изменить, запись
                   ждёт разбора, чужая правка не затирается
  link             привязать человека к гаражу (собственник / контакт)

Не прошедшее проверку не теряется: статус review, правление решает на
странице /offline/entries (повторить, отклонить, отметить сделанным на
сайте). Смену собственника при продаже (с архивом счетов прежнего и
переносом баланса) из поля не проводим — человек создаётся, а запись
ждёт, пока председатель оформит смену на сайте.
"""
import datetime as dt
import json
import os
import uuid as uuid_mod
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from flask_wtf.csrf import generate_csrf

from . import audit, database
from .accounting import reallocate_garage_charges, reallocate_member_charges
from .auth import roles_required
from .i18n import translate as _, parse_decimal
from .images import shrink_image
from .models import (
    ControlMeter, ControlMeterReading, ElectricityMeter, FieldEntry, Garage, GarageContact, GarageOwnership,
    MemberAccount, MeterPhoto, Payment, Person, Phone, RoleEnum,
)
from .permissions import is_board, is_privileged

bp = Blueprint("offline_sync", __name__, url_prefix="/offline")

KINDS = ("reading", "control_reading", "meter", "payment", "person_create", "person_edit", "link")
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_ENTRIES = 200

KIND_LABELS = {
    "reading": "Показания счётчика", "control_reading": "Показания контрольного счётчика",
    "meter": "Новый счётчик", "payment": "Оплата", "person_create": "Новый человек",
    "person_edit": "Правка карточки", "link": "Привязка к гаражу",
}
STATUS_LABELS = {
    "applied": "внесено", "pending": "ждёт подтверждения", "review": "требует разбора",
    "rejected": "отклонено", "done": "сделано на сайте",
}
# Поля карточки, которые можно задать без сети. При правке — только те, что
# есть в слепке (у телефона есть их старое значение для сверки).
PERSON_FIELDS = ("full_name", "email", "telegram", "max_messenger", "comment")
PERSON_CREATE_EXTRA = (
    "registration_address", "residence_address", "birth_date", "passport_series", "passport_number",
    "passport_issue_date", "membership_start_date",
)
DATE_FIELDS = {"birth_date", "passport_issue_date", "membership_start_date"}


class EntryProblem(Exception):
    """Запись не внесена — текст для правления; status — review или pending."""

    def __init__(self, message: str, status: str = "review", keep: bool = False):
        super().__init__(message)
        self.status = status
        self.keep = keep  # то, что уже сделано до проблемы, оставить (новый человек без привязки)


# ---------------------------------------------------------------------------
# Разбор значений из payload
# ---------------------------------------------------------------------------

def _decimal(value, what: str) -> Decimal:
    try:
        return parse_decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise EntryProblem(f"Некорректное значение: {what}.")


def _date(value, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise EntryProblem(f"Некорректная дата: {what}.")


def _garage(payload) -> Garage:
    garage = database.db_session.get(Garage, payload.get("garage_id") or 0)
    if garage is None:
        raise EntryProblem("Гараж не найден (удалён на сайте?).")
    return garage


def _person(person_id) -> Person:
    person = database.db_session.get(Person, person_id or 0)
    if person is None:
        raise EntryProblem("Человек не найден (удалён на сайте?).")
    return person


def _who(entry: FieldEntry) -> str:
    """Кто принял/записал. Телефон кладёт в запись имя того, кто её сделал:
    если отправил другой (первый вышел из учётной записи, не отправив) —
    видно обоих."""
    sender = entry.user.username if entry.user else "?"
    try:
        captured = json.loads(entry.payload).get("captured_by")
    except ValueError:
        captured = None
    if captured and captured != sender:
        return f"{captured} (отправил {sender})"
    return sender


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _via_max(entry: FieldEntry) -> bool:
    try:
        return json.loads(entry.payload).get("source") == "max"
    except ValueError:
        return False


def _field_note(entry: FieldEntry, day: dt.date | None = None) -> str:
    """Пометка в комментарии: откуда запись и когда сделана."""
    when = (day or entry.captured_at.date()).strftime("%d.%m.%Y")
    how = "через MAX" if _via_max(entry) else "без сети"
    return f"внесено {how} ({_who(entry)}, {when})"


# ---------------------------------------------------------------------------
# Внесение по видам
# ---------------------------------------------------------------------------

def _apply_reading(entry: FieldEntry, p: dict) -> tuple[str, int, str]:
    from .garages import ReadingError, _current_meter, record_electricity_reading

    garage = _garage(p)
    meter = _current_meter(garage)
    if meter is None:
        raise EntryProblem(f"У гаража №{garage.number} нет счётчика — сначала внесите счётчик.")
    if p.get("meter_number") and str(p["meter_number"]).strip() != meter.meter_number.strip():
        raise EntryProblem(
            f"Показания сняты со счётчика {p['meter_number']}, а у гаража №{garage.number} сейчас счётчик "
            f"{meter.meter_number}. Если счётчик заменили — внесите новый счётчик и повторите."
        )
    value = _decimal(p.get("value"), "показания")
    day = _date(p.get("date"), "дата снятия")
    comment = (p.get("comment") or "").strip()
    comment = f"{comment} ({_field_note(entry, day)})" if comment else _upper_first(_field_note(entry, day))
    try:
        reading, amount = record_electricity_reading(garage, meter, value, day, comment)
    except ReadingError as e:
        raise EntryProblem(str(e))
    if entry.photo_path:
        database.db_session.add(MeterPhoto(
            uploaded_by_user_id=entry.user_id, file_path=entry.photo_path, garage_id=garage.id,
            meter_id=meter.id, reading_id=reading.id, taken_date=day,
        ))
    message = f"Гараж №{garage.number}: {value}" + (f", начислено {amount} ₽" if amount is not None else "")
    return "electricity_reading", reading.id, message


def _apply_control_reading(entry: FieldEntry, p: dict) -> tuple[str, int, str]:
    node = database.db_session.get(ControlMeter, p.get("control_meter_id") or 0)
    if node is None:
        raise EntryProblem("Контрольный счётчик не найден (удалён на сайте?).")
    value = _decimal(p.get("value"), "показания")
    day = _date(p.get("date"), "дата снятия")
    if day > dt.date.today():
        raise EntryProblem("Дата снятия показаний не может быть в будущем.")
    previous = (
        database.db_session.query(ControlMeterReading)
        .filter_by(control_meter_id=node.id)
        .order_by(ControlMeterReading.reading_date.desc(), ControlMeterReading.id.desc())
        .first()
    )
    if previous is not None and value < previous.reading:
        raise EntryProblem(
            f"Показания не могут быть меньше предыдущих ({previous.reading.quantize(Decimal('0.01'))} "
            f"от {previous.reading_date.strftime('%d.%m.%Y')})."
        )
    comment = (p.get("comment") or "").strip()
    reading = ControlMeterReading(
        control_meter_id=node.id, reading=value, reading_date=day,
        comment=f"{comment} ({_field_note(entry, day)})" if comment else _upper_first(_field_note(entry, day)),
    )
    database.db_session.add(reading)
    database.db_session.flush()
    audit.record(
        "control_meter.reading_add", f"Внесены показания узла «{node.name}»: {value} (без сети)",
        entity_type="control_meter", entity_id=node.id,
    )
    return "control_meter_reading", reading.id, f"«{node.name}»: {value}"


def _apply_meter(entry: FieldEntry, p: dict) -> tuple[str, int, str]:
    from .garages import _current_meter

    garage = _garage(p)
    number = (p.get("meter_number") or "").strip()
    if not number:
        raise EntryProblem("Не указан номер счётчика.")
    current = _current_meter(garage)
    if current is not None and current.meter_number.strip() == number:
        return "electricity_meter", current.id, f"Гараж №{garage.number}: счётчик {number} уже внесён"
    installed = _date(p["installed_date"], "дата установки") if p.get("installed_date") else entry.captured_at.date()
    if current is not None and current.installed_date and installed < current.installed_date:
        raise EntryProblem(
            f"Дата установки раньше установки нынешнего счётчика {current.meter_number} "
            f"({current.installed_date.strftime('%d.%m.%Y')})."
        )
    comment = (p.get("comment") or "").strip()
    meter = ElectricityMeter(
        garage_id=garage.id, meter_number=number, installed_date=installed,
        sealed_date=_date(p["sealed_date"], "дата опломбировки") if p.get("sealed_date") else None,
        initial_reading=_decimal(p["initial_reading"], "начальные показания") if p.get("initial_reading") not in (None, "") else None,
        meter_seal_number=(p.get("meter_seal_number") or "").strip() or None,
        breaker_seal_number=(p.get("breaker_seal_number") or "").strip() or None,
        comment=f"{comment} ({_field_note(entry)})" if comment else _upper_first(_field_note(entry)),
    )
    database.db_session.add(meter)
    database.db_session.flush()
    audit.record(
        "electricity_meter.add", f"Добавлен счётчик гаража №{garage.number}: {number} (без сети)",
        entity_type="garage", entity_id=garage.id,
    )
    return "electricity_meter", meter.id, f"Гараж №{garage.number}: счётчик {number}"


def _payment_comment(entry: FieldEntry, p: dict) -> str:
    parts = [f"Наличные, квитанция №{entry.receipt_number}" if entry.receipt_number else "Наличные"]
    parts.append(f"принял {_who(entry)} " + ("через MAX" if _via_max(entry) else "без сети"))
    if p.get("payer_name"):
        parts.append(f"плательщик {p['payer_name']}")
    text = ", ".join(parts)
    if (p.get("comment") or "").strip():
        text += f": {p['comment'].strip()}"
    return text


def _apply_payment(entry: FieldEntry, p: dict, confirm: bool = False) -> tuple[str, int, str]:
    from . import notifications
    from .i18n import fmt2, format_date

    amount = _decimal(p.get("amount"), "сумма")
    if amount <= 0:
        raise EntryProblem("Сумма оплаты должна быть больше нуля.")
    day = _date(p.get("date"), "дата оплаты")
    target = p.get("target")
    allowed = entry.user is not None and entry.user.role in (RoleEnum.CHAIRMAN, RoleEnum.ACCOUNTANT)
    if not (allowed or confirm):
        raise EntryProblem(
            f"Принято {amount} ₽ (квитанция №{entry.receipt_number}). Проводит председатель или бухгалтер.",
            status="pending",
        )
    comment = _payment_comment(entry, p)
    if target == "electricity":
        garage = _garage(p)
        payer = p.get("payer_person_id")
        payment = Payment(
            garage_id=garage.id, date=day, amount=amount, comment=comment,
            payer_person_id=payer if payer and database.db_session.get(Person, payer) else None,
        )
        database.db_session.add(payment)
        database.db_session.flush()
        reallocate_garage_charges(garage)
        audit.record(
            "payment.create", entity_type="garage", entity_id=garage.id,
            summary=f"Платёж {audit.format_amount(amount)} на гараж №{garage.number} от {audit.format_date(day)}"
                    f" (наличные без сети, квитанция №{entry.receipt_number})",
        )
        return "payment", payment.id, f"Электричество, гараж №{garage.number}: {amount} ₽"
    if target == "account":
        account = database.db_session.get(MemberAccount, p.get("account_id") or 0)
        if account is None or account.is_archived:
            raise EntryProblem("Лицевой счёт не найден или закрыт — внесите оплату на сайте на нужный счёт.")
        payment = Payment(account_id=account.id, date=day, amount=amount,
                          comment=f"Л/С {account.account_number}: {comment}")
        database.db_session.add(payment)
        database.db_session.flush()
        reallocate_member_charges(account)
        audit.record(
            "payment.create", entity_type="member_account", entity_id=account.id,
            summary=f"Платёж {audit.format_amount(amount)} на счёт {account.account_number} "
                    f"({account.person.short_name}) от {audit.format_date(day)}"
                    f" (наличные без сети, квитанция №{entry.receipt_number})",
        )
        try:
            notifications.notify(
                notifications.user_for_person(account.person_id), "payment", "Платёж зачтён на ваш счёт",
                f"На счёт {account.account_number} зачтён платёж {fmt2(amount)} ₽ от {format_date(day)}.",
                url=notifications.path_for("finance.member_account_detail", account_id=account.id),
            )
        except Exception:  # уведомление — не повод не провести платёж
            current_app.logger.exception("field entry %s: payment notification failed", entry.uuid)
        return "payment", payment.id, f"{account.fee_type.name}, счёт {account.account_number}: {amount} ₽"
    raise EntryProblem("Не указано, за что оплата.")


def _link(entry: FieldEntry, person: Person, link: dict | None) -> str:
    """Привязка человека к гаражу из записи (новый человек или «link»).
    Возвращает текст итога; смена собственника — EntryProblem."""
    from .garages import add_garage_contact, add_garage_owner

    if not link or not link.get("garage_id"):
        return ""
    garage = _garage(link)
    role = link.get("role")
    if role == "contact":
        exists = database.db_session.query(GarageContact).filter_by(garage_id=garage.id, person_id=person.id).first()
        if exists is None:
            add_garage_contact(garage, person.id, (link.get("relation") or "").strip() or None, bool(link.get("tenant")))
        return f"лицо для связи гаража №{garage.number}"
    if role in ("owner", "coowner"):
        owners = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).all()
        if role == "owner" and any(o.person_id != person.id for o in owners):
            raise EntryProblem(
                f"У гаража №{garage.number} уже есть собственник. Если гараж продан — оформите смену "
                f"собственника на сайте; если это совладелец — повторите, указав долю."
            )
        share = _decimal(link.get("share") or "1", "доля")
        if not (0 < share <= 1):
            raise EntryProblem("Доля должна быть больше 0 и не больше 1.")
        add_garage_owner(garage, person.id, share, _field_note(entry), entry.user_id)
        return f"собственник гаража №{garage.number}" + (f", доля {share}" if share != 1 else "")
    if role == "replace":
        raise EntryProblem(
            f"Смена собственника гаража №{garage.number} (продажа): оформите её на сайте на карточке гаража "
            f"— там же архив счетов прежнего собственника и перенос баланса."
        )
    raise EntryProblem("Не указано, кем человек приходится гаражу.")


def _name_key(name: str) -> str:
    return " ".join((name or "").replace("ё", "е").replace("Ё", "Е").casefold().split())


def _same_name(name: str) -> Person | None:
    """Человек с тем же ФИО — без учёта регистра и «ё»/«е» (ilike в SQLite
    регистр кириллицы не сводит, поэтому сравнение здесь, в Python)."""
    key = _name_key(name)
    for person_id, full_name in database.db_session.query(Person.id, Person.full_name):
        if _name_key(full_name) == key:
            return database.db_session.get(Person, person_id)
    return None


def _apply_person_create(entry: FieldEntry, p: dict) -> tuple[str, int, str]:
    fields = p.get("fields") or {}
    name = " ".join((fields.get("full_name") or "").split())
    if not name:
        raise EntryProblem("Не указаны фамилия, имя и отчество.")
    person = None
    if entry.entity_type == "person" and entry.entity_id:  # человек уже создан прошлой попыткой
        person = database.db_session.get(Person, entry.entity_id)
    if person is None:
        same = _same_name(name)
        if same is not None and not p.get("allow_duplicate"):
            raise EntryProblem(
                f"Человек «{name}» уже есть в реестре — проверьте, не он ли это. Если это другой человек "
                f"с тем же именем — «Повторить всё равно»."
            )
        person = Person(full_name=name)
        for key in PERSON_FIELDS[1:] + PERSON_CREATE_EXTRA:
            value = fields.get(key)
            if isinstance(value, str):
                value = value.strip()
            if not value:
                continue
            setattr(person, key, _date(value, key) if key in DATE_FIELDS else value)
        note = _upper_first(_field_note(entry))
        person.comment = f"{person.comment}\n{note}" if person.comment else note
        database.db_session.add(person)
        database.db_session.flush()
        for number in p.get("phones") or []:
            if str(number).strip():
                database.db_session.add(Phone(person_id=person.id, number=str(number).strip()))
        audit.record("person.create", entity_type="person", entity_id=person.id,
                     summary=f"Добавлен человек: {person.full_name} (без сети)")
        # Человек остаётся, даже если привязка ниже уйдёт на разбор.
        entry.entity_type, entry.entity_id = "person", person.id
    link_point = database.db_session.begin_nested()
    try:
        linked = _link(entry, person, p.get("link"))
    except EntryProblem as problem:
        link_point.rollback()
        raise EntryProblem(f"Человек «{person.full_name}» внесён в реестр, но не привязан к гаражу. {problem}", keep=True)
    link_point.commit()
    return "person", person.id, person.full_name + (f": {linked}" if linked else "")


def _phones_value(person: Person) -> list[str]:
    return [ph.number for ph in person.phones]


def _apply_person_edit(entry: FieldEntry, p: dict, force: bool = False) -> tuple[str, int, str]:
    person = _person(p.get("person_id"))
    changes = p.get("changes") or {}
    conflicts = []
    for key, pair in changes.items():
        if key not in PERSON_FIELDS + ("phones",):
            continue
        old, new = pair
        current = _phones_value(person) if key == "phones" else getattr(person, key)
        norm = (lambda v: [str(x).strip() for x in v or []]) if key == "phones" else (lambda v: (v or "").strip())
        if norm(current) not in (norm(old), norm(new)):
            conflicts.append((key, current, new))
    if conflicts and not force:
        raise EntryProblem("Пока вы были без сети, на сайте изменили: " + "; ".join(
            f"{k}: на сайте «{', '.join(c) if isinstance(c, list) else c or '—'}», у вас «{', '.join(n) if isinstance(n, list) else n or '—'}»"
            for k, c, n in conflicts
        ) + ". «Применить мои» — записать ваши значения.")
    changed = []
    for key, (old, new) in changes.items():
        if key == "phones":
            for phone in list(person.phones):
                database.db_session.delete(phone)
            for number in new or []:
                if str(number).strip():
                    database.db_session.add(Phone(person_id=person.id, number=str(number).strip()))
            changed.append(key)
        elif key in PERSON_FIELDS:
            value = (new or "").strip() or None
            if key == "full_name" and not value:
                continue
            setattr(person, key, value)
            changed.append(key)
    audit.record("person.edit", entity_type="person", entity_id=person.id,
                 summary=f"Изменена карточка человека: {person.full_name} (без сети)")
    return "person", person.id, person.full_name


def _apply_link(entry: FieldEntry, p: dict) -> tuple[str, int, str]:
    person = _person(p.get("person_id"))
    return "person", person.id, f"{person.full_name}: {_link(entry, person, p.get('link'))}"


def apply_entry(entry: FieldEntry, force: bool = False, confirm: bool = False) -> None:
    """Внести запись в учёт; итог — в entry.status/message/entity_*.
    Изменения — в savepoint: ошибка одной записи не трогает остальные."""
    p = json.loads(entry.payload)
    savepoint = database.db_session.begin_nested()
    try:
        if entry.kind == "reading":
            result = _apply_reading(entry, p)
        elif entry.kind == "control_reading":
            result = _apply_control_reading(entry, p)
        elif entry.kind == "meter":
            result = _apply_meter(entry, p)
        elif entry.kind == "payment":
            result = _apply_payment(entry, p, confirm=confirm)
        elif entry.kind == "person_create":
            if force:
                p["allow_duplicate"] = True
            result = _apply_person_create(entry, p)
        elif entry.kind == "person_edit":
            result = _apply_person_edit(entry, p, force=force)
        elif entry.kind == "link":
            result = _apply_link(entry, p)
        else:
            raise EntryProblem("Неизвестный вид записи.")
    except EntryProblem as problem:
        if problem.keep:
            savepoint.commit()
        else:
            savepoint.rollback()
        entry.status, entry.message = problem.status, str(problem)
        return
    savepoint.commit()
    entry.status = "applied"
    entry.entity_type, entry.entity_id, entry.message = result


# ---------------------------------------------------------------------------
# Приём с телефона
# ---------------------------------------------------------------------------

def _entry_result(entry: FieldEntry) -> dict:
    return {"uuid": entry.uuid, "status": entry.status, "message": entry.message}


def _save_photo(file_storage) -> str | None:
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in PHOTO_EXT:
        ext = ".jpg"
    stored = f"{uuid_mod.uuid4().hex}{ext}"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    file_storage.save(path)
    shrink_image(path)
    return stored


def _receipt_ok(value) -> str | None:
    text = str(value or "").strip()
    return text[:30] or None


def last_receipt_seq(user_id: int) -> int:
    """Последний номер квитанции этого человека (часть после дефиса) —
    телефон продолжает нумерацию с него (слепок, app/offline.py)."""
    numbers = database.db_session.query(FieldEntry.receipt_number).filter(
        FieldEntry.user_id == user_id, FieldEntry.receipt_number.isnot(None),
    ).all()
    best = 0
    for (number,) in numbers:
        tail = number.rsplit("-", 1)[-1]
        if tail.isdigit():
            best = max(best, int(tail))
    return best


@bp.route("/csrf")
def csrf_token():
    """CSRF-токен для отправки очереди — телефон берёт его прямо перед
    отправкой (оболочка /offline/ закеширована и своего токена не имеет)."""
    if g.user is None:
        return jsonify(error="login required"), 401
    resp = jsonify(token=generate_csrf())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/sync", methods=["POST"])
def sync():
    if g.user is None:
        return jsonify(error="login required"), 401
    if not is_board():
        return jsonify(error="board only"), 403
    try:
        items = json.loads(request.form.get("entries") or "[]")
    except ValueError:
        return jsonify(error="bad entries"), 400
    if not isinstance(items, list) or len(items) > MAX_ENTRIES:
        return jsonify(error="bad entries"), 400

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uuid") or "")[:36]
        kind = item.get("kind")
        if not uid or kind not in KINDS or not isinstance(item.get("payload"), dict):
            results.append({"uuid": uid, "status": "rejected", "message": "Неизвестная запись."})
            continue
        entry = database.db_session.query(FieldEntry).filter_by(uuid=uid).first()
        if entry is not None:  # уже присылали — итог прежний
            results.append(_entry_result(entry))
            continue
        try:
            captured = dt.datetime.fromisoformat(str(item.get("captured_at"))[:19])
        except ValueError:
            captured = dt.datetime.now()
        entry = FieldEntry(
            uuid=uid, kind=kind, status="review", user_id=g.user.id, captured_at=captured,
            payload=json.dumps(item["payload"], ensure_ascii=False),
            receipt_number=_receipt_ok(item["payload"].get("receipt")) if kind == "payment" else None,
        )
        if kind == "reading":
            entry.photo_path = _save_photo(request.files.get(f"photo_{uid}"))
        database.db_session.add(entry)
        database.db_session.flush()
        apply_entry(entry)
        database.db_session.commit()
        results.append(_entry_result(entry))
    return jsonify(results=results, receipt_seq=last_receipt_seq(g.user.id))


# ---------------------------------------------------------------------------
# Разбор правлением
# ---------------------------------------------------------------------------

def open_count() -> int:
    """Для колокольчика «Требует внимания» (app/attention.py): записи без
    сети, ждущие решения. Оплату к проведению видят председатель и
    бухгалтер, разбор остального — всё правление."""
    query = database.db_session.query(FieldEntry)
    if is_privileged():
        return query.filter(FieldEntry.status.in_(("review", "pending"))).count()
    return query.filter(FieldEntry.status == "review").count()


def describe(entry: FieldEntry) -> str:
    """Что записали — одной строкой для страницы разбора."""
    p = json.loads(entry.payload)
    garage = database.db_session.get(Garage, p.get("garage_id") or (p.get("link") or {}).get("garage_id") or 0)
    gnum = f"гараж №{garage.number}" if garage else ""
    if entry.kind == "reading":
        return f"{gnum}, счётчик {p.get('meter_number') or '—'}: {p.get('value')} на {p.get('date')}"
    if entry.kind == "control_reading":
        node = database.db_session.get(ControlMeter, p.get("control_meter_id") or 0)
        return f"«{node.name if node else '?'}»: {p.get('value')} на {p.get('date')}"
    if entry.kind == "meter":
        return f"{gnum}: счётчик {p.get('meter_number')}, начальные {p.get('initial_reading') or '—'}"
    if entry.kind == "payment":
        what = "электричество" if p.get("target") == "electricity" else "взнос"
        if p.get("target") == "account":
            acc = database.db_session.get(MemberAccount, p.get("account_id") or 0)
            if acc:
                what = f"{acc.fee_type.name}, счёт {acc.account_number}"
        return f"{p.get('amount')} ₽ — {what}, {gnum}, квитанция №{entry.receipt_number}, {p.get('date')}"
    if entry.kind == "person_create":
        return (p.get("fields") or {}).get("full_name", "") + (f" — {gnum}" if gnum else "")
    if entry.kind == "person_edit":
        person = database.db_session.get(Person, p.get("person_id") or 0)
        return (person.full_name if person else "?") + ": " + ", ".join((p.get("changes") or {}).keys())
    if entry.kind == "link":
        person = database.db_session.get(Person, p.get("person_id") or 0)
        return f"{person.full_name if person else '?'} — {gnum}"
    return ""


@bp.route("/entries")
@roles_required(RoleEnum.BOARD)
def entries():
    show_all = request.args.get("all") == "1"
    query = database.db_session.query(FieldEntry)
    if show_all:
        query = query.filter(FieldEntry.received_at >= dt.datetime.now() - dt.timedelta(days=60))
    else:
        query = query.filter(FieldEntry.status.in_(("review", "pending")))
    rows = query.order_by(FieldEntry.received_at.desc(), FieldEntry.id.desc()).limit(500).all()
    return render_template(
        "offline/entries.html", rows=rows, show_all=show_all, describe=describe,
        kind_labels=KIND_LABELS, status_labels=STATUS_LABELS,
    )


@bp.route("/entries/<int:entry_id>/<action>", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def entry_action(entry_id, action):
    entry = database.db_session.get(FieldEntry, entry_id)
    if entry is None:
        abort(404)
    if entry.status not in ("review", "pending"):
        flash(_("Эта запись уже разобрана."), "warning")
        return redirect(url_for("offline_sync.entries"))
    if entry.kind == "payment" and action in ("retry", "force") and not is_privileged():
        abort(403)
    if action in ("retry", "force"):
        apply_entry(entry, force=action == "force", confirm=entry.kind == "payment")
        if entry.status == "applied":
            entry.resolved_by_user_id, entry.resolved_at = g.user.id, dt.datetime.now()
            flash(_("Внесено: {result}", result=entry.message), "success")
        else:
            flash(entry.message, "danger")
    elif action in ("reject", "done"):
        reason = (request.form.get("reason") or "").strip()
        entry.status = "rejected" if action == "reject" else "done"
        entry.resolved_by_user_id, entry.resolved_at = g.user.id, dt.datetime.now()
        if reason:
            entry.message = f"{entry.message or ''}\n{reason}".strip()
        audit.record(
            "field_entry." + action, entity_type="field_entry", entity_id=entry.id,
            summary=f"Запись без сети ({KIND_LABELS.get(entry.kind, entry.kind)}, {_who(entry)}): "
                    f"{'отклонена' if action == 'reject' else 'отмечена сделанной на сайте'}"
                    + (f" — {reason}" if reason else ""),
        )
        flash(_("Запись отклонена.") if action == "reject" else _("Запись отмечена сделанной."), "success")
    else:
        abort(404)
    database.db_session.commit()
    return redirect(url_for("offline_sync.entries"))
