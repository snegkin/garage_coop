"""
Команды бота MAX для правления — в личном диалоге с ботом. На территории
ГСК мобильный интернет только по белым спискам: сайт недоступен, а MAX
работает, поэтому через бота можно посмотреть гараж и внести данные с
живой базой (без сети совсем — оффлайн-страница, app/offline.py).

  15                   карточка гаража: собственники, телефоны, долги, счётчик
  15 1450              показания счётчика гаража 15 — с подтверждением кнопкой
  15 взнос 2000        принять оплату: кнопками — за что (электричество / счёт)
  фото счётчика        распознать (app/meter_photos.py) и предложить показания
  долги                список должников
  кооператив           выбрать кооператив (если человек в правлении нескольких)
  помощь

Показания и оплата вносятся так же, как записи без сети (app/offline_sync.py:
FieldEntry, apply_entry) — те же проверки, оплату рядового члена правления
проводят председатель или бухгалтер, не прошедшее проверку ждёт на
странице «Записи без сети». Кнопки подтверждения несут всё нужное в
payload (без состояния на сервере) и перепроверяют права при нажатии.

Работает в том кооперативе, где привязан аккаунт; при одном боте на
несколько кооперативов основной пересылает диалог нужному
(app/max_router.py).
"""
import datetime as dt
import json
import os
import re
import uuid as uuid_mod
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import requests
from flask import current_app, g, has_request_context

from . import database
from .auth import ROLE_LEVEL
from .models import (
    Cooperative, FieldEntry, Garage, MemberAccount, MeterPhoto, Person, RoleEnum, User,
)

PHOTO_TIMEOUT = 30
PHOTO_MAX_BYTES = 15 * 1024 * 1024
RECENT_PHOTO = dt.timedelta(hours=1)
DEBTORS_SHOWN = 20

HELP = (
    "Команды для правления:\n"
    "• 15 — гараж №15: собственники, телефоны, долги, счётчик\n"
    "• 15 1450 — показания счётчика гаража №15\n"
    "• 15 взнос 2000 — принять оплату (за что — выберете кнопкой)\n"
    "• фото счётчика — распознаю номер и показания\n"
    "• долги — список должников\n"
    "• кооператив — сменить кооператив, если вы в правлении нескольких"
)


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------

def _money(value) -> str:
    return f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _balance_text(value: Decimal) -> str:
    if value < 0:
        return f"долг {_money(-value)}"
    if value > 0:
        return f"переплата {_money(value)}"
    return "0 ₽"


def _coop_name() -> str:
    coop = database.db_session.query(Cooperative).first()
    return (coop.short_name or coop.full_name) if coop and (coop.short_name or coop.full_name) else "Кооператив"


def _number(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _fmt(value: Decimal) -> str:
    return f"{Decimal(value).normalize():f}"


def _user_for(max_user_id) -> tuple[Person | None, User | None]:
    person = database.db_session.query(Person).filter_by(max_user_id=max_user_id).first() if max_user_id else None
    user = database.db_session.query(User).filter_by(person_id=person.id).first() if person else None
    return person, user


def _is_board(user: User | None) -> bool:
    return bool(user and getattr(user, "is_active", True) and ROLE_LEVEL[user.role] >= ROLE_LEVEL[RoleEnum.BOARD])


@contextmanager
def _as_user(user: User):
    """Права проверяются так же, как на сайте (permissions.* смотрят g.user),
    url_for — в контексте запроса (cron работает без него)."""
    ctx = None if has_request_context() else current_app.test_request_context("/")
    if ctx:
        ctx.push()
    previous = g.get("user")
    g.user = user
    try:
        yield
    finally:
        g.user = previous
        if ctx:
            ctx.pop()


def _send(settings, max_user_id, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
    from . import max_bot

    attachments = None
    if buttons:
        attachments = [{"type": "inline_keyboard", "payload": {"buttons": [
            [{"type": "callback", "text": label[:64], "payload": _tagged(payload)} for label, payload in row] for row in buttons
        ]}}]
    try:
        max_bot.send_message(settings, None, f"{_coop_name()}\n{text}", attachments=attachments, user_id=max_user_id)
    except max_bot.MaxError as exc:
        current_app.logger.warning("MAX: ответ в диалог не отправлен: %s", exc)


def send_plain(settings, max_user_id, text: str, buttons=None) -> None:
    """Сообщение без названия кооператива и без метки в кнопках — для
    выбора кооператива (app/max_router.py)."""
    from . import max_bot

    attachments = None
    if buttons:
        attachments = [{"type": "inline_keyboard", "payload": {"buttons": [
            [{"type": "callback", "text": label[:64], "payload": payload} for label, payload in row] for row in buttons
        ]}}]
    try:
        max_bot.send_message(settings, None, text, attachments=attachments, user_id=max_user_id)
    except max_bot.MaxError as exc:
        current_app.logger.warning("MAX: сообщение в диалог не отправлено: %s", exc)


def answer_plain(settings, callback_id: str, text: str) -> None:
    from . import max_bot

    try:
        max_bot._request(settings, "POST", "/answers", idempotent=True, params={"callback_id": callback_id},
                         payload={"message": {"text": text, "attachments": []}})
    except max_bot.MaxError as exc:
        current_app.logger.warning("MAX: ответ на кнопку не отправлен: %s", exc)


def _tagged(payload: str) -> str:
    """Кнопка помнит, какой кооператив её показал: основной кооператив
    отдаёт нажатие именно ему (app/max_router.py), даже если человек
    тем временем переключился на другой."""
    from .max_router import coop_code

    code = coop_code()
    return f"{code}:{payload}" if code else payload


def _answer(settings, callback_id: str, text: str) -> None:
    """Ответ на нажатие кнопки — заменить сообщение с кнопками итогом."""
    from . import max_bot

    try:
        max_bot._request(settings, "POST", "/answers", idempotent=True, params={"callback_id": callback_id},
                         payload={"message": {"text": f"{_coop_name()}\n{text}", "attachments": []}})
    except max_bot.MaxError as exc:
        current_app.logger.warning("MAX: ответ на кнопку не отправлен: %s", exc)


def _find_garage(text: str) -> Garage | None:
    key = text.strip().lower().replace("№", "")
    if not key:
        return None
    for garage in database.db_session.query(Garage):
        if garage.number.strip().lower() == key:
            return garage
    return None


# ---------------------------------------------------------------------------
# Карточка гаража, должники
# ---------------------------------------------------------------------------

def garage_text(garage: Garage) -> str:
    from .accounting import balance
    from .garages import _current_meter, latest_reading
    from .permissions import is_privileged

    lines = [f"Гараж №{garage.number}" + (" (за территорией)" if garage.outside_territory else "")]
    for o in garage.ownerships:
        phones = ", ".join(p.number for p in o.person.phones) or "телефон не указан"
        share = f", доля {_fmt(o.share)}" if o.share < 1 else ""
        lines.append(f"Собственник: {o.person.full_name}{share} — {phones}")
    if not garage.ownerships:
        lines.append("Собственник не указан")
    for c in garage.contacts:
        phones = ", ".join(p.number for p in c.person.phones) or "телефон не указан"
        lines.append(f"Для связи: {c.person.full_name}" + (f" ({c.relation})" if c.relation else "") + f" — {phones}")
    if garage.charges or garage.payments:
        lines.append(f"Электричество: {_balance_text(balance(garage))}")
    meter = _current_meter(garage)
    if meter is None:
        lines.append("Счётчика нет")
    else:
        last = latest_reading(meter)
        lines.append(f"Счётчик {meter.meter_number}: " + (
            f"{_fmt(last.reading)} от {last.reading_date.strftime('%d.%m.%Y')}" if last else "показаний нет"))
    owner_ids = [o.person_id for o in garage.ownerships]
    accounts = database.db_session.query(MemberAccount).filter(
        MemberAccount.is_archived.is_(False),
        (MemberAccount.garage_id == garage.id)
        | (MemberAccount.garage_id.is_(None) & MemberAccount.person_id.in_(owner_ids or [0])),
    ).all()
    for a in sorted(accounts, key=lambda a: (a.fee_type.name, a.person.full_name)):
        if a.fee_type.is_penalty and not is_privileged():
            continue
        bal = balance(a)
        if bal != 0:
            lines.append(f"{a.fee_type.name} ({a.person.short_name}): {_balance_text(bal)}")
    return "\n".join(lines)


def debtors_text() -> str:
    from .legal_docs import list_debtor_persons

    rows = sorted(list_debtor_persons(), key=lambda r: r["debt"], reverse=True)
    if not rows:
        return "Должников нет."
    total = sum((r["debt"] for r in rows), Decimal("0"))
    lines = [f"Должников: {len(rows)}, всего {_money(total)}"]
    for r in rows[:DEBTORS_SHOWN]:
        lines.append(f"{r['person'].full_name}: {_money(r['debt'])}")
    if len(rows) > DEBTORS_SHOWN:
        lines.append(f"…и ещё {len(rows) - DEBTORS_SHOWN} — полный список на сайте.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Показания
# ---------------------------------------------------------------------------

def _recent_photo(user: User, garage: Garage | None = None) -> MeterPhoto | None:
    query = database.db_session.query(MeterPhoto).filter(
        MeterPhoto.uploaded_by_user_id == user.id, MeterPhoto.reading_id.is_(None),
        MeterPhoto.created_at >= dt.datetime.now() - RECENT_PHOTO,
    )
    photo = query.order_by(MeterPhoto.id.desc()).first()
    if photo and garage and photo.garage_id not in (None, garage.id):
        return None
    return photo


def ask_reading(settings, max_user_id, user: User, garage: Garage, value: Decimal, photo_id: int | None = None):
    from .garages import _current_meter, latest_reading

    meter = _current_meter(garage)
    if meter is None:
        _send(settings, max_user_id, f"У гаража №{garage.number} нет счётчика — внесите его на сайте.")
        return
    last = latest_reading(meter)
    baseline = last.reading if last else meter.initial_reading
    if baseline is not None and value <= baseline:
        _send(settings, max_user_id, f"Гараж №{garage.number}: показания должны быть больше прежних ({_fmt(baseline)}"
              + (f" от {last.reading_date.strftime('%d.%m.%Y')}" if last else "") + ").")
        return
    if photo_id is None:
        recent = _recent_photo(user, garage)
        photo_id = recent.id if recent else None
    used = f", расход {_fmt(value - baseline)} кВт·ч" if baseline is not None else ""
    today = dt.date.today().isoformat()
    _send(settings, max_user_id,
          f"Гараж №{garage.number}, счётчик {meter.meter_number}: внести {_fmt(value)}{used}?"
          + (" Фото приложу к показаниям." if photo_id else ""),
          [[("Внести", f"rd|{garage.id}|{_fmt(value)}|{today}|{photo_id or 0}|{meter.meter_number}"),
            ("Отмена", "no")]])


def _apply(kind: str, payload: dict, user: User, receipt: str | None = None) -> FieldEntry:
    """Внести как запись правления (те же проверки, что и записи без сети)."""
    from .offline_sync import apply_entry

    payload = dict(payload, source="max")
    entry = FieldEntry(
        uuid=f"max-{uuid_mod.uuid4().hex}", kind=kind, status="review", user_id=user.id,
        captured_at=dt.datetime.now(), payload=json.dumps(payload, ensure_ascii=False), receipt_number=receipt,
    )
    database.db_session.add(entry)
    database.db_session.flush()
    apply_entry(entry)
    return entry


def confirm_reading(user: User, parts: list[str]) -> str:
    _tag, garage_id, value, day, photo_id, meter_number = (parts + [""] * 6)[:6]
    entry = _apply("reading", {"garage_id": int(garage_id), "meter_number": meter_number, "value": value,
                               "date": day}, user)
    if entry.status == "applied" and photo_id and photo_id != "0":
        photo = database.db_session.get(MeterPhoto, int(photo_id))
        reading_id = entry.entity_id
        if photo is not None and photo.reading_id is None and photo.uploaded_by_user_id == user.id:
            from .models import ElectricityReading
            reading = database.db_session.get(ElectricityReading, reading_id)
            photo.garage_id, photo.meter_id, photo.reading_id = int(garage_id), reading.meter_id, reading.id
    if entry.status == "applied":
        return f"Внесено: {entry.message}"
    return f"Не внесено: {entry.message}\nЗапись ждёт решения на сайте («Записи без сети»)."


# ---------------------------------------------------------------------------
# Оплата
# ---------------------------------------------------------------------------

def ask_payment(settings, max_user_id, garage: Garage, amount: Decimal):
    from .accounting import balance

    if amount <= 0:
        _send(settings, max_user_id, "Сумма оплаты должна быть больше нуля.")
        return
    buttons = []
    if garage.meters or garage.charges or garage.payments:
        buttons.append([(f"Электричество ({_balance_text(balance(garage))})", f"pay|{garage.id}|el|{_fmt(amount)}")])
    owner_ids = [o.person_id for o in garage.ownerships]
    accounts = database.db_session.query(MemberAccount).filter(
        MemberAccount.is_archived.is_(False),
        (MemberAccount.garage_id == garage.id)
        | (MemberAccount.garage_id.is_(None) & MemberAccount.person_id.in_(owner_ids or [0])),
    ).all()
    for a in sorted(accounts, key=lambda a: (a.fee_type.is_penalty, a.fee_type.name)):
        label = f"{a.fee_type.name}, {a.person.short_name} ({_balance_text(balance(a))})"
        buttons.append([(label, f"pay|{garage.id}|a{a.id}|{_fmt(amount)}")])
    if not buttons:
        _send(settings, max_user_id, f"У гаража №{garage.number} нет счетов, на которые принять оплату.")
        return
    buttons.append([("Отмена", "no")])
    _send(settings, max_user_id, f"Гараж №{garage.number}: принять {_money(amount)} — за что?", buttons[:30])


def confirm_payment(user: User, parts: list[str]) -> str:
    from .offline_sync import last_receipt_seq

    _tag, garage_id, target, amount = (parts + [""] * 4)[:4]
    receipt = f"{user.id}-{last_receipt_seq(user.id) + 1:04d}"
    payload = {"garage_id": int(garage_id), "amount": amount, "date": dt.date.today().isoformat(), "receipt": receipt}
    if target == "el":
        payload["target"] = "electricity"
    else:
        payload.update(target="account", account_id=int(target[1:]))
    entry = _apply("payment", payload, user, receipt=receipt)
    head = f"Квитанция №{receipt}: {_money(Decimal(amount))}"
    if entry.status == "applied":
        return f"{head}\nПроведено: {entry.message}"
    if entry.status == "pending":
        return f"{head}\nПринято. Проведёт председатель или бухгалтер (страница «Записи без сети»)."
    return f"{head}\nНе проведено: {entry.message}"


# ---------------------------------------------------------------------------
# Фото счётчика
# ---------------------------------------------------------------------------

def _image_url(message: dict) -> str | None:
    for att in (message.get("body") or {}).get("attachments") or []:
        if att.get("type") == "image" and (att.get("payload") or {}).get("url"):
            return att["payload"]["url"]
    return None


def handle_photo(settings, max_user_id, user: User, url: str) -> None:
    from .images import shrink_image
    from .meter_photos import analyze, available_garages, run_ocr

    try:
        resp = requests.get(url, timeout=PHOTO_TIMEOUT, stream=True)
        resp.raise_for_status()
        content = resp.raw.read(PHOTO_MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        current_app.logger.warning("MAX: фото не скачалось: %s", exc)
        _send(settings, max_user_id, "Не удалось получить фото — пришлите ещё раз или напишите показания текстом.")
        return
    if len(content) > PHOTO_MAX_BYTES:
        _send(settings, max_user_id, "Фото слишком большое.")
        return
    stored = f"{uuid_mod.uuid4().hex}.jpg"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    with open(path, "wb") as f:
        f.write(content)
    ocr = run_ocr(path)
    shrink_image(path)
    photo = MeterPhoto(uploaded_by_user_id=user.id, file_path=stored,
                       ocr_tokens=json.dumps(ocr["tokens"], ensure_ascii=False) if "tokens" in ocr else None,
                       ocr_error=ocr.get("error"))
    database.db_session.add(photo)
    database.db_session.flush()
    result = analyze(photo, available_garages())
    photo.garage_id = result["garage_id"]
    photo.suggested_reading = Decimal(result["suggestion"]) if result["suggestion"] else None
    garage = database.db_session.get(Garage, result["garage_id"]) if result["garage_id"] else None
    if garage is None:
        _send(settings, max_user_id, "Счётчик на фото не узнал. Напишите номер гаража и показания, например: 15 1450"
              " — фото приложу к ним.")
        return
    lines = [f"Гараж №{garage.number}, счётчик {result['meter_number']}"
             + (" (номер на фото совпал)" if result["meter_status"] in ("matched", "similar") else "")]
    if result["baseline"]:
        lines.append(f"Прошлые показания: {_fmt(Decimal(result['baseline']))}"
                     + (f" от {dt.date.fromisoformat(result['baseline_date']).strftime('%d.%m.%Y')}" if result["baseline_date"] else ""))
    lines.extend(result["warnings"])
    options = [o["value"] for o in result["options"][:3]]
    today = dt.date.today().isoformat()
    buttons = [[(f"Внести {_fmt(Decimal(v))}",
                 f"rd|{garage.id}|{_fmt(Decimal(v))}|{today}|{photo.id}|{result['meter_number']}")] for v in options]
    lines.append("Выберите показания или напишите: " + f"{garage.number} <показания>")
    _send(settings, max_user_id, "\n".join(lines), buttons + [[("Отмена", "no")]])


# ---------------------------------------------------------------------------
# Вход: сообщение и нажатие кнопки
# ---------------------------------------------------------------------------

READING_RE = re.compile(r"^(\S+)\s+(\d[\d\s]*(?:[.,]\d+)?)$")
PAYMENT_RE = re.compile(r"^(\S+)\s+(?:взнос|оплата|платёж|платеж|оплатил|принял)\s+(\d[\d\s]*(?:[.,]\d+)?)\s*(?:р|руб|₽)?\.?$", re.I)


def handle_message(settings, update: dict) -> None:
    message = update.get("message") or {}
    sender = message.get("sender") or {}
    if sender.get("is_bot"):
        return
    max_user_id = sender.get("user_id")
    _person, user = _user_for(max_user_id)
    if user is None:
        _send(settings, max_user_id, "Этот аккаунт MAX не привязан к сайту кооператива. Привяжите его в профиле на сайте "
              "(«Мой профиль» → MAX) — сюда будут приходить уведомления.")
        return
    if not _is_board(user):
        _send(settings, max_user_id, "Уведомления с сайта приходят сюда. Команды бота — только для правления.")
        return
    text = " ".join(((message.get("body") or {}).get("text") or "").split())
    with _as_user(user):
        url = _image_url(message)
        if url:
            handle_photo(settings, max_user_id, user, url)
        elif not text or text.lower() in ("помощь", "/help", "/start", "?", "help", "start", "команды"):
            _send(settings, max_user_id, HELP)
        elif text.lower() in ("долги", "должники", "/debts"):
            _send(settings, max_user_id, debtors_text())
        elif text.lower() in ("кооператив", "/coop"):
            _send(settings, max_user_id, "Вы работаете с одним кооперативом.")
        else:
            m_pay = PAYMENT_RE.match(text)
            m_read = READING_RE.match(text)
            garage = _find_garage((m_pay or m_read).group(1) if (m_pay or m_read) else text)
            if garage is None:
                _send(settings, max_user_id, "Не понял. Напишите «помощь» — список команд.")
            elif m_pay:
                ask_payment(settings, max_user_id, garage, _number(m_pay.group(2)) or Decimal("0"))
            elif m_read:
                ask_reading(settings, max_user_id, user, garage, _number(m_read.group(2)) or Decimal("0"))
            else:
                _send(settings, max_user_id, garage_text(garage))
    database.db_session.commit()


def handle_callback(settings, update: dict) -> None:
    callback = update.get("callback") or {}
    callback_id = callback.get("callback_id")
    payload = callback.get("payload") or ""
    max_user_id = (callback.get("user") or {}).get("user_id")
    if not callback_id:
        return
    _person, user = _user_for(max_user_id)
    if not _is_board(user):
        _answer(settings, callback_id, "Команды бота — только для правления.")
        return
    from .max_router import coop_code

    code = coop_code()
    if code and payload.startswith(f"{code}:"):
        payload = payload[len(code) + 1:]
    parts = payload.split("|")
    with _as_user(user):
        if parts[0] == "rd" and len(parts) >= 6:
            result = confirm_reading(user, parts)
        elif parts[0] == "pay" and len(parts) >= 4:
            result = confirm_payment(user, parts)
        else:
            result = "Отменено."
    database.db_session.commit()
    _answer(settings, callback_id, result)
