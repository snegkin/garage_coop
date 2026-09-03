"""
Автоматическое начисление пени по просроченным взносам членов кооператива.

Формула (аналог ст. 155 ЖК РФ): сумма непогашенного остатка конкретного
начисления × ключевая ставка ЦБ РФ (действующая на этот день) × 1/300 —
для первых 30 дней просрочки, 1/150 — начиная с 31-го дня × 1 день,
просуммированная по всем дням просрочки.

Срок оплаты — единая по уставу дата в году (Cooperative.dues_due_day/month,
см. cooperative.edit). Ключевая ставка ЦБ РФ хранится в таблице key_rate
(история, см. models.KeyRate) — подгружается с cbr.ru (официальный SOAP-сервис
DailyInfoWebServ, метод KeyRate) либо вносится вручную, если сайт недоступен.

Расчёт идёт по каждому обычному (не пенному) начислению члена кооператива
отдельно, с учётом реальных дат частичных платежей (FIFO-разнесение —
см. accounting.reallocate_member_charges) — непогашенный остаток на каждый
день просрочки считается точно, а не по балансу счёта в целом. Повторные
запуски идемпотентны: каждое начисление помнит дату, по которую пеня уже
посчитана (Charge.penalty_calculated_through), и досчитывает только новые дни.
"""
import bisect
import datetime as dt
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import database
from .i18n import translate as _, parse_decimal
from .auth import roles_required
from .models import Charge, Cooperative, FeeType, KeyRate, MemberAccount, RoleEnum
from .accounting import dues_due_date, penalty_sibling_account, reallocate_member_charges

bp = Blueprint("penalty", __name__, url_prefix="/finance/penalty")

CBR_URL = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
CBR_SOAP_ACTION = "http://web.cbr.ru/KeyRate"


# ---------------------------------------------------------------------------
# Загрузка истории ключевой ставки с cbr.ru
# ---------------------------------------------------------------------------

def _local_tag(tag: str) -> str:
    """Имя тега без namespace-префикса ('{ns}Rate' -> 'Rate')."""
    return tag.rsplit("}", 1)[-1]


def _cbr_soap_envelope(from_date: dt.date, to_date: dt.date) -> bytes:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        '<KeyRate xmlns="http://web.cbr.ru/">'
        f"<fromDate>{from_date.isoformat()}T00:00:00</fromDate>"
        f"<ToDate>{to_date.isoformat()}T00:00:00</ToDate>"
        "</KeyRate>"
        "</soap:Body>"
        "</soap:Envelope>"
    )
    return body.encode("utf-8")


def fetch_key_rates_from_cbr(from_date: dt.date, to_date: dt.date, timeout: int = 15) -> list[tuple[dt.date, Decimal]]:
    """
    Запрашивает историю ключевой ставки ЦБ РФ за период через официальный
    SOAP-сервис (см. https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx?op=KeyRate).
    Возвращает [(дата_действия, ставка_%)], отсортировано по дате.
    Кидает исключение при сетевой ошибке или неожиданном формате ответа —
    вызывающий код (penalty.fetch_key_rate) обязан её поймать и показать
    пользователю понятное сообщение вместо падения с 500-й ошибкой.

    Ответ ASMX сериализует DataSet как настоящие вложенные XML-элементы
    (diffgram) прямо внутри <KeyRateResult> — не как экранированную строку —
    поэтому просто ищем по всему дереву ответа элементы-строки, у которых
    среди дочерних есть <DT> и <Rate> (без привязки к namespace-префиксам,
    т.к. схема не документирована формально и могла бы отличаться).
    """
    req = urllib.request.Request(
        CBR_URL,
        data=_cbr_soap_envelope(from_date, to_date),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{CBR_SOAP_ACTION}"',
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    rows: list[tuple[dt.date, Decimal]] = []
    for el in root.iter():
        children = {_local_tag(c.tag): (c.text or "").strip() for c in el}
        if children.get("DT") and children.get("Rate"):
            try:
                rate_date = dt.date.fromisoformat(children["DT"][:10])
                rate_value = Decimal(children["Rate"].replace(",", "."))
            except (ValueError, InvalidOperation):
                continue
            rows.append((rate_date, rate_value))

    if not rows:
        raise ValueError("Не удалось найти данные в ответе ЦБ РФ.")

    rows.sort(key=lambda r: r[0])
    return _compress_to_change_points(rows)


def _compress_to_change_points(rows: list[tuple[dt.date, Decimal]]) -> list[tuple[dt.date, Decimal]]:
    """
    cbr.ru отдаёт по записи на каждый календарный день, даже если ставка не
    менялась — а для расчёта пени (см. key_rate_on/compute_charge_penalty)
    важны только даты фактических изменений, промежуточные повторы значения
    не нужны и только раздувают таблицу. `rows` должен быть отсортирован по
    дате — оставляет только первую дату каждого нового значения.
    """
    compressed: list[tuple[dt.date, Decimal]] = []
    last_value: Decimal | None = None
    for eff_date, rate in rows:
        if last_value is None or rate != last_value:
            compressed.append((eff_date, rate))
            last_value = rate
    return compressed


def save_key_rates(rows: list[tuple[dt.date, Decimal]]) -> int:
    """
    Сохраняет загруженные с ЦБ ставки в базу — обновляет существующие записи
    (кроме внесённых вручную, is_manual=True — они в приоритете) и добавляет
    новые. `rows` уже сжат до точек изменения (см. _compress_to_change_points).
    Возвращает количество затронутых записей. Не коммитит сама.
    """
    existing = {r.effective_date: r for r in database.db_session.query(KeyRate).all()}
    touched = 0
    for eff_date, rate in rows:
        row = existing.get(eff_date)
        if row is None:
            database.db_session.add(KeyRate(effective_date=eff_date, rate_percent=rate, is_manual=False))
            touched += 1
        elif not row.is_manual and row.rate_percent != rate:
            row.rate_percent = rate
            touched += 1
    return touched


def compact_key_rates() -> int:
    """
    Проход по уже сохранённой истории ставки — убирает записи, которые не
    меняют действующее значение по сравнению с предыдущей (сохранённой)
    записью. Нужен по двум причинам: (1) старые данные, загруженные до
    появления сжатия на входе (_compress_to_change_points), могли остаться
    «по записи на день»; (2) даже со сжатием на входе возможен шов на
    границе двух отдельных загрузок — первая дата новой загрузки может
    совпасть по значению с последней датой предыдущей. Ручные записи
    (is_manual=True) никогда не удаляются, даже если совпадают по значению
    с соседней — это явное решение правления. Возвращает количество
    удалённых записей. Не коммитит сама.
    """
    rows = database.db_session.query(KeyRate).order_by(KeyRate.effective_date, KeyRate.id).all()
    removed = 0
    last_value: Decimal | None = None
    for row in rows:
        if row.is_manual:
            last_value = row.rate_percent
            continue
        if last_value is not None and row.rate_percent == last_value:
            database.db_session.delete(row)
            removed += 1
        else:
            last_value = row.rate_percent
    return removed


# ---------------------------------------------------------------------------
# Расчёт пени
# ---------------------------------------------------------------------------

def _rate_on(dates: list[dt.date], rates: list[Decimal], as_of: dt.date) -> Decimal | None:
    """dates отсортирован по возрастанию — находим последнюю ставку не позже as_of."""
    idx = bisect.bisect_right(dates, as_of) - 1
    return rates[idx] if idx >= 0 else None


def compute_charge_penalty(
    charge: Charge, coop: Cooperative, target_date: dt.date,
    key_dates: list[dt.date], key_rates: list[Decimal],
) -> tuple[Decimal, dt.date | None]:
    """
    Считает пеню по одному начислению за новые дни просрочки (с прошлого
    расчёта, если он был, иначе с первого дня после срока оплаты) по
    target_date включительно. Возвращает (сумма, дата_по_которую_учтено) —
    вторая координата None, если срок оплаты не настроен или считать нечего
    (начисление ещё не просрочено / уже полностью учтено ранее).
    """
    due = dues_due_date(coop, charge.year)
    if due is None:
        return Decimal("0"), None

    start = due + dt.timedelta(days=1)
    if charge.penalty_calculated_through is not None and charge.penalty_calculated_through >= start:
        start = charge.penalty_calculated_through + dt.timedelta(days=1)
    if start > target_date:
        return Decimal("0"), None

    allocations = sorted(
        ((a.payment.date, a.amount) for a in charge.allocations), key=lambda x: x[0]
    )

    total = Decimal("0")
    d = start
    one_day = dt.timedelta(days=1)
    while d <= target_date:
        paid = sum((amt for pdate, amt in allocations if pdate <= d), Decimal("0"))
        unpaid = charge.amount - paid
        if unpaid > 0:
            rate = _rate_on(key_dates, key_rates, d)
            if rate is not None:
                day_index = (d - due).days
                divisor = Decimal(300) if day_index <= 30 else Decimal(150)
                total += unpaid * (rate / Decimal("100")) / divisor
        d += one_day

    return total.quantize(Decimal("0.01")), target_date


def compute_charge_penalty_breakdown(
    charge: Charge, coop: Cooperative, target_date: dt.date,
    key_dates: list[dt.date], key_rates: list[Decimal],
) -> list[dict]:
    """
    Тот же день-за-днём проход, что и compute_charge_penalty, но вместо
    одной суммы возвращает построчную раскладку по периодам — для
    официального расчёта пени, который истец (кооператив) прикладывает к
    исковому заявлению (см. app/templates/persons/penalty_calculation.html):
    период (с — по), число дней, непогашенный остаток, ставка ЦБ РФ,
    знаменатель (300 или 150), сумма пени за период. Дни с одинаковым
    (остаток, ставка, знаменатель) схлопываются в одну строку; смена
    любого из них (частичный платёж изменил остаток, ставка ЦБ изменилась,
    наступил 31-й день просрочки) начинает новую строку.

    В отличие от compute_charge_penalty — считает ВСЕГДА с самого первого
    дня просрочки по target_date включительно, не учитывает
    penalty_calculated_through (это поле — только для идемпотентности
    бухгалтерских проводок accrue_penalties, для судебного расчёта нужна
    вся история целиком, а не только «новые» дни с последнего запуска
    начисления). Не пишет ничего в БД.
    """
    due = dues_due_date(coop, charge.year)
    if due is None:
        return []
    start = due + dt.timedelta(days=1)
    if start > target_date:
        return []

    allocations = sorted(
        ((a.payment.date, a.amount) for a in charge.allocations), key=lambda x: x[0]
    )

    periods: list[dict] = []
    current: dict | None = None
    d = start
    one_day = dt.timedelta(days=1)
    while d <= target_date:
        paid = sum((amt for pdate, amt in allocations if pdate <= d), Decimal("0"))
        unpaid = charge.amount - paid
        if unpaid > 0:
            rate = _rate_on(key_dates, key_rates, d)
            if rate is not None:
                day_index = (d - due).days
                divisor = Decimal(300) if day_index <= 30 else Decimal(150)
                day_amount = unpaid * (rate / Decimal("100")) / divisor
                if current is not None and current["unpaid"] == unpaid and current["rate"] == rate and current["divisor"] == divisor:
                    current["end"] = d
                    current["days"] += 1
                    current["amount"] += day_amount
                else:
                    if current is not None:
                        periods.append(current)
                    current = {
                        "start": d, "end": d, "days": 1,
                        "unpaid": unpaid, "rate": rate, "divisor": divisor, "amount": day_amount,
                    }
        d += one_day
    if current is not None:
        periods.append(current)

    for p in periods:
        p["amount"] = p["amount"].quantize(Decimal("0.01"))
    return periods


def accrue_penalties(target_date: dt.date | None = None) -> dict:
    """
    Начисляет пеню по всем просроченным начислениям членов кооператива
    (обычные — не пенные — начисления на MemberAccount). Идемпотентна:
    повторный запуск на ту же (или более раннюю) дату не создаёт дублей —
    досчитывает только новые дни с прошлого запуска. Коммитит сама.

    Вызывается ТОЛЬКО скриптом scripts/accrue_penalty.py по cron (раз в
    месяц, см. README.md) — раньше запускалась ещё и тихо на каждом
    открытии дашборда/страницы «Пеня», но от этого отказались: начисления
    происходили неочевидно для правления (просто открыл страницу — и уже
    что-то начислилось) и раздували историю начислений построчно почти на
    каждый день, если в систему заходили часто. Раз в месяц — то же самое
    итоговое начисление (день-в-день, ставка та же), но одной строкой на
    начисление за период, а не десятками.

    Возвращает словарь с результатами или {"error": "no_due_date" / "no_key_rate"}.
    """
    target_date = target_date or dt.date.today()
    coop = database.db_session.query(Cooperative).first()
    if coop is None or dues_due_date(coop, target_date.year) is None:
        return {"error": "no_due_date"}

    key_rows = database.db_session.query(KeyRate).order_by(KeyRate.effective_date).all()
    if not key_rows:
        return {"error": "no_key_rate"}
    key_dates = [r.effective_date for r in key_rows]
    key_rates = [r.rate_percent for r in key_rows]

    charges = (
        database.db_session.query(Charge)
        .join(MemberAccount, Charge.account_id == MemberAccount.id)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(FeeType.is_penalty.is_(False))
        .all()
    )

    charged_rows = []          # (person_name, sibling_account_number, charge_year, amount)
    skipped_rows = []          # (person_name, account_number, charge_year) — нет счёта пени
    total = Decimal("0")
    siblings_to_reallocate: dict[int, MemberAccount] = {}

    for charge in charges:
        account = charge.account
        amount, through = compute_charge_penalty(charge, coop, target_date, key_dates, key_rates)
        if through is None:
            continue
        charge.penalty_calculated_through = through
        if amount <= 0:
            continue
        sibling = penalty_sibling_account(account)
        if sibling is None:
            skipped_rows.append((account.person.full_name, account.account_number, charge.year))
            continue
        database.db_session.add(Charge(
            account_id=sibling.id,
            year=target_date.year,
            amount=amount,
            penalty_for_charge_id=charge.id,
            comment=f"Пеня по начислению №{charge.id} за {charge.year} г., по {through.isoformat()}",
        ))
        charged_rows.append((account.person.full_name, sibling.account_number, charge.year, amount))
        total += amount
        siblings_to_reallocate[sibling.id] = sibling

    database.db_session.flush()
    for sibling in siblings_to_reallocate.values():
        reallocate_member_charges(sibling)
    database.db_session.commit()

    return {
        "target_date": target_date,
        "charged_rows": charged_rows,
        "skipped_rows": skipped_rows,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------

@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    coop = database.db_session.query(Cooperative).first()
    due_date_this_year = dues_due_date(coop, dt.date.today().year) if coop else None

    rates = database.db_session.query(KeyRate).order_by(KeyRate.effective_date.desc()).all()

    # "С даты" в форме загрузки с ЦБ подставляется от самой свежей загруженной
    # ставки в БД целиком.
    latest_rate = database.db_session.query(KeyRate).order_by(KeyRate.effective_date.desc()).first()
    suggested_from_date = (latest_rate.effective_date + dt.timedelta(days=1)) if latest_rate else dt.date(2020, 1, 1)

    return render_template(
        "finance/penalty.html",
        coop=coop, due_date_this_year=due_date_this_year, rates=rates,
        today=dt.date.today(), suggested_from_date=suggested_from_date,
    )


@bp.route("/key-rate/fetch", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def fetch_key_rate():
    f = request.form
    try:
        from_date = dt.date.fromisoformat(f["from_date"])
        to_date = dt.date.fromisoformat(f["to_date"]) if f.get("to_date") else dt.date.today()
    except ValueError:
        flash(_("Некорректные даты."), "danger")
        return redirect(url_for("penalty.view"))

    try:
        rows = fetch_key_rates_from_cbr(from_date, to_date)
    except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError) as exc:
        flash(_("Не удалось загрузить ставку с cbr.ru: {error}. Можно внести значение вручную ниже.", error=str(exc)), "danger")
        return redirect(url_for("penalty.view"))

    if not rows:
        flash(_("ЦБ РФ не вернул ни одной записи за указанный период."), "warning")
        return redirect(url_for("penalty.view"))

    touched = save_key_rates(rows)
    compacted = compact_key_rates()
    database.db_session.commit()
    msg = _("Загружено записей ключевой ставки: {n}.", n=touched)
    if compacted:
        msg += " " + _("Убрано избыточных (без изменения значения): {n}.", n=compacted)
    flash(msg, "success")
    return redirect(url_for("penalty.view"))


@bp.route("/key-rate/manual", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_manual_key_rate():
    f = request.form
    effective_date = dt.date.fromisoformat(f["effective_date"])
    rate_percent = parse_decimal(f["rate_percent"])

    row = database.db_session.query(KeyRate).filter_by(effective_date=effective_date).first()
    if row is None:
        row = KeyRate(effective_date=effective_date)
        database.db_session.add(row)
    row.rate_percent = rate_percent
    row.is_manual = True
    database.db_session.commit()
    flash(_("Ставка на {date} сохранена вручную.", date=effective_date.strftime("%d.%m.%Y")), "success")
    return redirect(url_for("penalty.view"))


@bp.route("/key-rate/<int:rate_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_key_rate(rate_id):
    row = database.db_session.get(KeyRate, rate_id)
    if row is not None:
        database.db_session.delete(row)
        database.db_session.commit()
        flash(_("Запись ставки удалена."), "success")
    return redirect(url_for("penalty.view"))
