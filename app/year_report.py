"""
«Финансы за год» (`/year-report/`, только председатель) — сводка в духе
годового бухгалтерского отчёта: сколько планировалось получить
(начислено) и сколько получено, сколько планировалось потратить (смета —
BudgetItem) и сколько потрачено и на что (расходы у контрагентов —
Expense, сгруппированные по статьям).

Здесь же — инструменты, чтобы данные о тратах были полными:
- «Списания без расхода» — строки выписки банка (списания), к которым ещё
  не заведён платёж контрагенту: одной кнопкой заводятся расход и платёж,
  привязанный к этой строке (adjusts_bank_balance=False — деньги уже
  списаны, баланс счёта их не содержит), контрагент и статья подбираются
  по ИНН/названию/назначению платежа;
- загрузка сметы из CSV (выгрузка таблицы председателя — разделы и статьи);
- импорт «Отчёта по тратам» из CSV с предпросмотром: строки сопоставляются
  с уже заведёнными расходами и со списаниями из выписки, остальное
  заводится как «оплачено не через банк» (наличными, за свой счёт).

Статьи расходов — Expense.category, свободный текст; для сводки он
приводится к статье справочника ExpenseCategory (normalize_category) — исторические
«электротовары»/«Хостинг»/«штраф» и т.п. попадают в общие статьи.
"""
import csv
import datetime as dt
import io
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload

from . import audit, database
from .accounting import available_statement_lines, pay_counterparty, reallocate_counterparty_expenses
from .auth import roles_required
from .i18n import translate as _
from .models import (
    BankAccount, BankStatementLine, BudgetItem, Charge, Counterparty, CounterpartyPayment, CsvImportProfile, Expense,
    BudgetSection, ChargeAllocation, ExpenseCategory, ExpenseKind, FeeType, MemberAccount, Payment, RoleEnum,
)

bp = Blueprint("year_report", __name__, url_prefix="/year-report")

# Статьи расходов — справочник ExpenseCategory (страница «Статьи и цвета»,
# см. ниже categories_view). Expense.category хранит НАЗВАНИЕ статьи как
# есть, по-русски — это данные кооператива, не строки интерфейса.
# Значения по умолчанию — те же, что заносит миграция fb2d330cafd9: ими
# пользуются, пока справочник пуст или нет контекста приложения (разбор
# CSV в тестах без БД).
OTHER_CATEGORY = "Прочее"
DEFAULT_CATEGORIES = [
    # (название, цвет, слова для автоподбора, не входит в смету, к смете по статье, по умолчанию)
    ("Земельный налог", "#2a78d6", "земельн, единый налоговый платеж, единый налоговый платёж", True, False, False),
    ("Банковское обслуживание", "#a8743a", "комисси, платежные поручения, платёжные поручения", False, True, False),
    ("Оплата труда", "#e87ba4", 'фонд оплаты труда, "фот", документооборот, оплата труда', False, False, False),
    ("Отчётность и ЭДО", "#86b6ef", 'сбис, нулевк, отчетност, отчётност, скзи, "кэп", тензор, крипто-про', False, True, False),
    ("Налоги, штрафы, пени", "#e34948", 'штраф, "пени", "пеня", "налог", "налоги", "налога", "налогам"', False, True, False),
    ("Юридические расходы", "#b5476b", "госпошлин, исков, юр. помощь, юридич", False, False, False),
    ("Почтовые расходы", "#f4a582", "почтов, уфпс, почта росс", False, True, False),
    ("Электроэнергия", "#eda100", "электроэнерг, тнс энерго", False, True, False),
    ("Сайт и связь", "#1baf7a",
     "домен, хостинг, vps, бегет, рег.ру, смс, sms, мобильн, связь, интернет-услуг, сайт, коммуникац",
     False, False, False),
    ("Хоз. работы", "#008300",
     "снег, покос, спил, кустар, кустов, субботник, мусор, гербиц, подсыпк, крошк, транспортн, "
     "хоз. работ, хоз.работ, хозработ", False, False, False),
    ("Видеонаблюдение", "#6250d6",
     "камер, видеонабл, wi-fi мост, ubiquiti, litebeam, poe, витая пара, коммутатор, powerline",
     False, False, False),
    ("Электрика", "#eb6834",
     "провод, кабел, гильз, сжим, клемм, изолент, щит, din-рейк, фонар, опор, sonoff, автоматическ, электротовар",
     False, False, False),
    ("Канцелярия", "#b3a9ef", "бумаг, степлер, скоб, картридж, резак, трафарет, магнит", False, False, False),
    (OTHER_CATEGORY, "#a3a29d", None, False, False, True),
]
GRAY = "#a3a29d"


def parse_keywords(text: str | None) -> list[re.Pattern]:
    """Слова для автоподбора — через запятую или с новой строки. Слово
    совпадает с НАЧАЛОМ слова в тексте («провод» — «проводки», но не
    «щит» в «защитный»); в кавычках — только слово целиком («"фот"» — не
    «фотореле»)."""
    patterns = []
    for raw in re.split(r"[,\n]", text or ""):
        word = raw.strip()
        whole = len(word) > 2 and word[0] in "\"«" and word[-1] in "\"»"
        word = word.strip("\"«» ").lower()
        if not word:
            continue
        tail = r"(?![\w])" if whole else ""
        patterns.append(re.compile(r"(?<![\w])" + re.escape(word) + tail, re.IGNORECASE))
    return patterns


class CategoryRules:
    """Справочник статей в виде, удобном для подбора: порядок, цвета,
    признаки и скомпилированные слова."""

    def __init__(self, rows):
        rows = list(rows)
        self.names = [r[0] for r in rows]
        self.colors = {r[0]: r[1] or GRAY for r in rows}
        self.matchers = [(r[0], parse_keywords(r[2])) for r in rows]
        self.outside = {r[0] for r in rows if r[3]}
        self.narrow = {r[0] for r in rows if r[4]}
        self.default = next((r[0] for r in rows if r[5]), OTHER_CATEGORY)
        self._by_lower = {name.lower(): name for name in self.names}

    def exact(self, text: str | None) -> str | None:
        return self._by_lower.get((text or "").strip().lower())

    def by_keywords(self, text: str | None) -> str | None:
        for name, patterns in self.matchers:
            if any(p.search(text or "") for p in patterns):
                return name
        return None

    def all_matching(self, text: str | None) -> set[str]:
        return {name for name, patterns in self.matchers if any(p.search(text or "") for p in patterns)}


def category_rules() -> CategoryRules:
    """Справочник статей — из БД (раз на запрос), по умолчанию — встроенный."""
    try:
        cached = getattr(g, "_expense_category_rules", None)
    except RuntimeError:  # нет контекста приложения
        return CategoryRules(DEFAULT_CATEGORIES)
    if cached is None:
        rows = database.db_session.query(ExpenseCategory).order_by(ExpenseCategory.position, ExpenseCategory.id).all()
        cached = CategoryRules(
            [(c.name, c.color, c.keywords, c.outside_budget, c.budget_match_by_category, c.is_default) for c in rows]
            or DEFAULT_CATEGORIES
        )
        g._expense_category_rules = cached
    return cached


# Контрагент для трат, оплаченных не со счёта кооператива (наличными,
# председателем за свой счёт и т.п.) — заводится при импорте отчёта по
# тратам, если такого ещё нет (ищется по названию).
CASH_COUNTERPARTY_NAME = "Оплачено не через банк (наличные, за свой счёт)"

# Технические проводки — не реальные деньги, а перенос сальдо между
# лицевыми счетами (зачёт, выбытие/смена собственника) и списание пени:
# в «получено»/«начислено» не входят. Комментарии пишутся при создании
# проводки (accounting.py/finance.py/penalty.py) на языке кооператива.
_TECH_PAYMENT_PREFIXES = (
    "Долг передан", "Зачёт со счёта", "Списание пени", "Переплата, унаследованная", "Доля переплаты",
)
_TECH_CHARGE_PREFIXES = (
    "Зачёт на счёт", "Долг, унаследованный", "Переплата передана", "Доля долга",
)


def cap(value: str | None, column) -> str | None:
    """Обрезать строку по длине колонки модели (String(n)) — значения из
    форм и CSV не должны выходить за размер поля (см. app/database.py:
    _check_string_lengths)."""
    if value is None:
        return None
    length = getattr(column.type, "length", None)
    return value[:length] if length else value


def normalize_category(raw: str | None) -> str:
    """Свободный текст Expense.category → статья справочника (по названию,
    иначе по словам автоподбора) или сам текст, если он ни на что не похож
    — тогда статья показывается как есть."""
    rules = category_rules()
    if not raw or not raw.strip():
        return rules.default
    return rules.exact(raw) or rules.by_keywords(raw) or raw.strip()


def _category_by_keywords(text: str) -> str | None:
    return category_rules().by_keywords(text)


def guess_category(text: str, section_hint: str | None = None) -> str:
    """Статья для новой траты: раздел отчёта, если он сам по себе статья
    («видеонаблюдение» — даже щит в этом разделе относится к нему), иначе
    по словам автоподбора, иначе статья по умолчанию («Прочее»)."""
    rules = category_rules()
    if section_hint:
        hinted = rules.exact(section_hint) or rules.by_keywords(section_hint)
        if hinted and hinted != rules.default:
            return hinted
    return rules.by_keywords(text) or rules.default


# --- Деньги и даты из CSV --------------------------------------------------

def parse_money(value: str | None) -> Decimal | None:
    """« р. 4 103,50 » / «2 500,00» / « р. -   » → Decimal или None."""
    if value is None:
        return None
    text = value.replace("р.", "").replace("₽", "").replace("\xa0", "").replace(" ", "").strip()
    if not text or text in ("-", "—"):
        return None
    text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_ru_date(value: str | None) -> dt.date | None:
    match = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", value or "")
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _read_csv_rows(text: str, width: int) -> list[list[str]]:
    rows = []
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        rows.append([cell.strip() for cell in row] + [""] * (width - len(row)))
    return rows


def _decode_upload(file_storage) -> str | None:
    if not file_storage or not file_storage.filename:
        return None
    raw = file_storage.read()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


# --- Смета -------------------------------------------------------------------

# Настраиваемые столбцы CSV — как в импорте людей и гаражей мастера
# настройки (CsvImportProfile, общая модалка setup/_csv_format_modal.html),
# но позиция здесь — абсолютный номер столбца в файле: в выгрузке отчёта
# по тратам между наименованием и датой есть второй столбец, который в
# одних блоках пустой, в других — месяц потребления, и пропуск столбца не
# должен сдвигать остальные. Профиль хранит список ключей по порядку
# столбцов файла, "" — столбец, который не читается.
# Каталог: (ключ, подпись, обязателен ли).
BUDGET_COLUMN_CATALOG = [
    ("name", "Наименование", True),
    ("quantity", "Кол-во", False),
    ("price", "Цена", False),
    ("amount", "Сумма", True),
]
SPENDING_COLUMN_CATALOG = [
    ("name", "Наименование", True),
    ("extra", "Номер заказа или месяц потребления", False),
    ("date", "Дата", True),
    ("quantity", "Кол-во", False),
    ("price", "Цена", False),
    ("amount", "Сумма", True),
]
CSV_FORMATS = {
    "budget": ("year_budget", BUDGET_COLUMN_CATALOG),
    "spending": ("year_spending", SPENDING_COLUMN_CATALOG),
}
_EXAMPLE_ROWS = {
    "budget": [
        {"name": "Содержание кооператива"},
        {"name": "Механизированная уборка снега", "quantity": "6", "price": "2 500,00", "amount": "15 000,00"},
        {"name": "Фонд оплаты труда", "quantity": "12", "price": "2 000,00", "amount": "24 000,00"},
    ],
    "spending": [
        {"name": "Наименование (электроэнергия)", "date": "Дата", "amount": "Сумма"},
        {"name": "", "extra": "янв", "date": "13.02.2026", "quantity": "725", "price": "5,66", "amount": "4 103,50"},
        {"name": "Наименование (видеонаблюдение)", "date": "Дата", "amount": "Сумма"},
        {"name": "Wi-Fi мост", "extra": "0161382282-0005", "date": "29.07.2026", "quantity": "3", "price": "3 653,00", "amount": "10 959,00"},
        {"name": "Механизированная уборка снега", "date": "20.01.2026", "quantity": "2", "price": "2 500,00", "amount": "5 000,00"},
    ],
}


def get_csv_columns(kind: str) -> list[str]:
    """Ключи по порядку столбцов файла ("" — пропускаемый столбец). По
    умолчанию — каталог по порядку (формат таблиц председателя)."""
    import_type, catalog = CSV_FORMATS[kind]
    valid = {k for k, _l, _r in catalog}
    profile = database.db_session.query(CsvImportProfile).filter_by(import_type=import_type).first()
    if profile:
        try:
            cols = [c if c in valid else "" for c in json.loads(profile.columns)]
        except (ValueError, TypeError):
            cols = []
        if any(cols):
            return cols
    return [k for k, _l, _r in catalog]


def _row_values(row: list[str], columns: list[str]) -> dict[str, str]:
    return {key: (row[i].strip() if i < len(row) else "") for i, key in enumerate(columns) if key}


def parse_budget_csv(text: str, columns: list[str] | None = None) -> list[dict]:
    """
    Смета из CSV: строка с одним только наименованием (без количества,
    цены и суммы) — раздел, строка с суммой — статья, строка без
    наименования — подытог раздела (пропускается), строка заголовков
    («Наименование, Кол-во, …» — в столбцах текст вместо чисел) тоже.
    Хвост с итогами («Количество гаражей», «Целевая сумма»…) не читается.
    """
    columns = columns or [k for k, _l, _r in BUDGET_COLUMN_CATALOG]
    items = []
    section = None
    for row in _read_csv_rows(text, len(columns)):
        v = _row_values(row, columns)
        name, qty, price, amount = v.get("name", ""), v.get("quantity", ""), v.get("price", ""), v.get("amount", "")
        if name.startswith("Количество гаражей") or name.startswith("Целевая сумма"):
            break
        if not name:
            continue
        amount_value = parse_money(amount)
        if not amount and not qty and not price:
            section = name
            continue
        if amount_value is None or amount_value <= 0:
            continue
        items.append({
            "section": cap(section or OTHER_CATEGORY, BudgetItem.__table__.c.section),
            "name": name,
            "quantity": parse_money(qty),
            "price": parse_money(price),
            "amount": amount_value,
        })
    return items


# --- Отчёт по тратам ------------------------------------------------------

_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _month_end(year: int, month: int) -> dt.date:
    if month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, month + 1, 1) - dt.timedelta(days=1)


def parse_spending_csv(text: str, columns: list[str] | None = None) -> list[dict]:
    """
    «Отчёт по тратам» из CSV: блоки трат, каждый начинается строкой
    заголовка «Наименование (раздел), , Дата, …» (наименование есть, а в
    столбцах даты и суммы — не дата и не сумма), в конце блока — «Итого»
    (без даты, пропускается). Столбец «номер заказа или месяц»: месяц
    («янв») — строка электроэнергии, дата траты тогда последний день этого
    месяца (как дата счёта ТНС), а дата в строке — день оплаты (paid_on, по
    ней ищется списание в выписке); номер заказа маркетплейса — по нему
    позиции заказа сводятся к одной оплате. Читается до «Легенды».
    Пустые/нулевые строки (субботник силами членов) пропускаются.
    """
    columns = columns or [k for k, _l, _r in SPENDING_COLUMN_CATALOG]
    rows = []
    section = None
    for index, row in enumerate(_read_csv_rows(text, len(columns))):
        v = _row_values(row, columns)
        name, extra, date_col = v.get("name", ""), v.get("extra", ""), v.get("date", "")
        amount = v.get("amount", "")
        if name.startswith("Легенда"):
            break
        paid_on = _parse_ru_date(date_col)
        amount_value = parse_money(amount)
        if name and paid_on is None and date_col and amount_value is None:
            header = re.search(r"\(([^)]*)\)", name)
            section = header.group(1).strip() if header else name
            continue
        if paid_on is None or amount_value is None or amount_value <= 0:
            continue
        date = paid_on
        month_key = extra.lower()[:3]
        if month_key in _MONTHS and not re.search(r"\d", extra):
            month = _MONTHS.index(month_key) + 1
            year = paid_on.year if month <= paid_on.month else paid_on.year - 1
            date = _month_end(year, month)
            title = section.capitalize() if section else "Электроэнергия"
            name = f"{title} за {extra.lower()} {year}"
        if not name:
            continue
        quantity = parse_money(v.get("quantity"))
        price_value = parse_money(v.get("price"))
        description = name
        if quantity is not None and price_value is not None and quantity != 1:
            description = f"{name} ({_fmt_number(quantity)} × {_fmt_number(price_value)})"
        order = extra if re.fullmatch(r"\d{6,}-\d{1,6}", extra) else None
        rows.append({
            "index": index,
            "order": order,
            "section": section,
            "name": name,
            "description": description,
            "date": date,
            "paid_on": paid_on,
            "amount": amount_value,
            "category": guess_category(name, section),
        })
    return rows


def _fmt_money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_number(value: Decimal) -> str:
    text = f"{value:,.3f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")
    return text


# --- Списания из выписки --------------------------------------------------

_LEGAL_FORMS = re.compile(
    r"общество с ограниченной ответственностью|публичное акционерное общество|акционерное общество"
    r"|\bооо\b|\bпао\b|\bоао\b|\bао\b|\bип\b|компания",
)


def _norm_name(name: str | None) -> str:
    text = (name or "").lower().replace("ё", "е")
    text = re.sub(r"[\"'«»“”„()]", " ", text)
    text = _LEGAL_FORMS.sub(" ", text)
    text = re.sub(r"[,.]", " ", text)
    return " ".join(text.split())


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def clean_counterparty_name(line: BankStatementLine) -> str:
    """Название нового контрагента по строке выписки: у перевода по СБП
    физлицу — имя получателя из назначения, иначе название из выписки без
    лишних кавычек по краям (Сбер иногда отдаёт «'ООО "..."'»)."""
    purpose = line.payment_purpose or ""
    sbp = re.search(r"Перевод СБП ФЛ\s+(.+?)\s+по\s", purpose)
    if sbp:
        return cap(sbp.group(1).strip(), Counterparty.__table__.c.name)
    name = (line.counterparty_name or "").strip()
    if len(name) > 1 and name[0] == name[-1] and name[0] in "'\"":
        name = name[1:-1].strip()
    name = re.sub(r"(?i)^общество с ограниченной ответственностью", "ООО", name)
    name = re.sub(r"(?i)^публичное акционерное общество", "ПАО", name)
    name = re.sub(r"(?i)^акционерное общество", "АО", name)
    return name or _("Без названия")


def match_counterparty(line: BankStatementLine, counterparties: list[Counterparty]) -> Counterparty | None:
    """Контрагент из справочника для строки выписки: по ИНН, иначе по
    названию (одно содержит другое после отбрасывания ООО/ПАО/кавычек —
    «ФНС» ↔ «Казначейство России (ФНС России)»). Перевод по СБП физлицу
    идёт через Сбербанк с его ИНН — по ИНН такой не сопоставляется."""
    purpose = line.payment_purpose or ""
    is_sbp_person = "Перевод СБП ФЛ" in purpose
    inn = _digits(line.counterparty_inn)
    if inn and not is_sbp_person:
        for cp in counterparties:
            if _digits(cp.inn) == inn:
                return cp
    target = _norm_name(clean_counterparty_name(line))
    if not target:
        return None
    for cp in counterparties:
        cp_name = _norm_name(cp.name)
        if cp_name and len(cp_name) >= 3 and (cp_name in target or target in cp_name):
            return cp
    return None


def bill_date(line: BankStatementLine) -> dt.date:
    """Дата расхода по строке выписки — дата счёта из назначения платежа
    («По счету №… от 31.01.26»), если она не позже дня оплаты и не раньше
    чем за 4 месяца до него (иначе это не дата счёта), иначе день оплаты.
    Для ТНС это последний день месяца потребления — и электроэнергия за
    декабрь попадает в декабрь, а не в январь, когда её оплатили."""
    purpose = line.payment_purpose or ""
    candidates = []
    for d, m, y in re.findall(r"от\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})", purpose):
        year = int(y) + (2000 if len(y) == 2 else 0)
        candidates.append((year, int(m), int(d)))
    for y, m, d in re.findall(r"от\s+(\d{4})-(\d{2})-(\d{2})", purpose):
        candidates.append((int(y), int(m), int(d)))
    for year, month, day in candidates:
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        if 0 <= (line.operation_date - date).days <= 120:
            return date
    return line.operation_date


def _own_account_digits() -> set[str]:
    return {_digits(a.checking_account) for a in database.db_session.query(BankAccount).all() if a.checking_account}


def unaccounted_debits(year: int | None = None) -> list[BankStatementLine]:
    """Списания из выписки без платежа контрагенту, кроме отмеченных «не
    трата» и переводов между своими счетами кооператива."""
    own = _own_account_digits()
    lines = [
        line for line in available_statement_lines()
        if line.direction == "debit" and not line.not_expense
        and _digits(line.counterparty_account) not in own
    ]
    if year is not None:
        lines = [line for line in lines if line.operation_date.year == year]
    return sorted(lines, key=lambda line: (line.operation_date, line.id))


def suggest_for_line(line: BankStatementLine, counterparties: list[Counterparty]) -> dict:
    counterparty = match_counterparty(line, counterparties)
    text = " ".join(filter(None, [line.counterparty_name, line.payment_purpose]))
    category = _category_by_keywords(text)
    if category is None and counterparty is not None and counterparty.category:
        alias = normalize_category(counterparty.category)
        category = alias if alias in category_rules().names else None
    confident = category is not None
    if category is None and counterparty is not None:
        # Как уже разнесены прошлые расходы этого контрагента — самая частая
        # статья. Только подсказка в списке, не «уверенная»: у маркетплейса
        # (Озон) прошлые заказы ничего не говорят о следующем, и «Завести
        # все» такую строку не берёт.
        counts: dict[str, int] = {}
        for e in counterparty.expenses:
            c = normalize_category(e.category)
            counts[c] = counts.get(c, 0) + 1
        if counts:
            category = max(counts, key=counts.get)
    return {
        "line": line,
        "counterparty": counterparty,
        "new_name": clean_counterparty_name(line),
        "category": category or category_rules().default,
        "confident": confident,
        "description": (line.payment_purpose or "").strip()[:500],
        "date": bill_date(line),
    }


def _get_or_create_counterparty(counterparty_id: str | None, line: BankStatementLine | None, new_name: str | None,
                                record: bool = True) -> Counterparty:
    """Контрагент из формы или новый по строке выписки. record=False — при
    массовом заведении/импорте: новые контрагенты упоминаются в одной
    сводной записи журнала, а не каждый отдельной (и отдельным
    уведомлением правлению)."""
    if counterparty_id and counterparty_id != "new":
        cp = database.db_session.get(Counterparty, int(counterparty_id))
        if cp is None:
            abort(400)
        return cp
    name = cap((new_name or "").strip() or (clean_counterparty_name(line) if line else _("Без названия")),
               Counterparty.__table__.c.name)
    inn = None
    if line is not None and "Перевод СБП ФЛ" not in (line.payment_purpose or ""):
        inn = _digits(line.counterparty_inn) or None
    cp = Counterparty(name=name, inn=inn)
    database.db_session.add(cp)
    database.db_session.flush()
    if record:
        audit.record("counterparty.create", entity_type="counterparty", entity_id=cp.id,
                     summary=f"Контрагент «{name}» заведён по строке выписки банка")
    return cp


def _cash_counterparty() -> Counterparty:
    cp = database.db_session.query(Counterparty).filter(Counterparty.name == CASH_COUNTERPARTY_NAME).first()
    if cp is None:
        cp = Counterparty(name=CASH_COUNTERPARTY_NAME, comment=_(
            "Траты, оплаченные не со счёта кооператива: наличными, за свой счёт с последующим возмещением и т.п."
        ))
        database.db_session.add(cp)
        database.db_session.flush()
    return cp


def book_line(line: BankStatementLine, counterparty: Counterparty, category: str, description: str | None,
              date: dt.date | None = None) -> Expense:
    """Расход на всю сумму строки выписки + платёж, привязанный к ней."""
    expense = Expense(
        counterparty_id=counterparty.id, date=date or bill_date(line), amount=line.amount,
        category=category, description=description or None, kind=ExpenseKind.CHARGE,
    )
    database.db_session.add(expense)
    database.db_session.flush()
    pay_counterparty(
        counterparty, line.operation_date, line.amount, bank_account=line.bank_account,
        comment=(line.payment_purpose or "").strip()[:500] or None, adjust_balance=False,
        bank_statement_line_id=line.id,
    )
    return expense


# --- Сводка за год -------------------------------------------------------

def _is_tech(comment: str | None, prefixes: tuple[str, ...]) -> bool:
    return bool(comment) and comment.startswith(prefixes)


def _available_years() -> list[int]:
    years = {y for (y,) in database.db_session.query(Charge.year).distinct()}
    years |= {d.year for (d,) in database.db_session.query(Expense.date).all()}
    years |= {y for (y,) in database.db_session.query(BudgetItem.year).distinct()}
    years |= {d.year for (d,) in database.db_session.query(BankStatementLine.operation_date).all()}
    years.add(dt.date.today().year)
    return sorted(years)


def _income(year: int) -> dict:
    """Начислено за год и получено в течение года (по дате платежа) — по
    видам взноса; пеня — одной строкой «Пеня» на все виды."""
    fee_types = {ft.id: ft for ft in database.db_session.query(FeeType).all()}
    electricity = next((ft for ft in fee_types.values() if ft.code == "electricity"), None)
    electricity_name = electricity.name if electricity else _("Электроэнергия")
    penalty_name = _("Пеня")

    def _name_for(fee_type: FeeType | None, garage_level: bool) -> str:
        if fee_type is None:
            return electricity_name if garage_level else OTHER_CATEGORY
        return penalty_name if fee_type.is_penalty else fee_type.name

    rows: dict[str, dict] = {}

    penalty_color = next((ft.color for ft in fee_types.values() if ft.is_penalty and ft.color), None)

    def _color_for(fee_type: FeeType | None, garage_level: bool) -> str | None:
        if fee_type is None:
            return (electricity.color if electricity else None) if garage_level else None
        return penalty_color if fee_type.is_penalty else fee_type.color

    def _row(name: str, code: str | None = None, color: str | None = None) -> dict:
        row = rows.setdefault(name, {"name": name, "code": code, "color": None,
                                     "charged": Decimal("0"), "received": Decimal("0")})
        row["color"] = row["color"] or color
        return row

    def _code_for(fee_type: FeeType | None, garage_level: bool) -> str | None:
        if fee_type is None:
            return "electricity" if garage_level else None
        return "penalty" if fee_type.is_penalty else fee_type.code

    charges = (
        database.db_session.query(Charge)
        .options(selectinload(Charge.account).selectinload(MemberAccount.fee_type), selectinload(Charge.fee_type))
        .filter(Charge.year == year)
        .all()
    )
    for charge in charges:
        if charge.related_person_id is not None or _is_tech(charge.comment, _TECH_CHARGE_PREFIXES):
            continue
        fee_type = charge.account.fee_type if charge.account is not None else charge.fee_type
        _row(_name_for(fee_type, charge.account is None), _code_for(fee_type, charge.account is None),
             _color_for(fee_type, charge.account is None))["charged"] += charge.amount

    payments = (
        database.db_session.query(Payment)
        .options(selectinload(Payment.account).selectinload(MemberAccount.fee_type))
        .filter(Payment.date >= dt.date(year, 1, 1), Payment.date <= dt.date(year, 12, 31))
        .all()
    )
    for payment in payments:
        if (payment.related_person_id is not None or payment.offset_charge_id is not None
                or payment.amnesty_for_charge_id is not None
                or _is_tech(payment.comment, _TECH_PAYMENT_PREFIXES)):
            continue
        fee_type = payment.account.fee_type if payment.account is not None else None
        _row(_name_for(fee_type, payment.account is None), _code_for(fee_type, payment.account is None),
             _color_for(fee_type, payment.account is None))["received"] += payment.amount

    ordered = sorted(rows.values(), key=lambda r: (-r["received"], -r["charged"]))
    for r in ordered:
        r["rate"] = _percent(r["received"], r["charged"])
    total = {
        "charged": sum((r["charged"] for r in ordered), Decimal("0")),
        "received": sum((r["received"] for r in ordered), Decimal("0")),
    }
    total["rate"] = _percent(total["received"], total["charged"])
    return {"rows": ordered, "total": total}


def _percent(part: Decimal, whole: Decimal) -> float | None:
    if not whole:
        return None
    return float((part / whole * 100).quantize(Decimal("0.1")))


def _spending(year: int) -> dict:
    """Расходы (начисления контрагентов, kind=CHARGE — счета на предоплату
    тратой не являются, тратой станут акты по ним) с датой в этом году, по
    статьям. paid — сколько из них уже оплачено (разнесено платежами)."""
    expenses = (
        database.db_session.query(Expense)
        .options(selectinload(Expense.counterparty), selectinload(Expense.allocations))
        .filter(Expense.date >= dt.date(year, 1, 1), Expense.date <= dt.date(year, 12, 31),
                Expense.kind == ExpenseKind.CHARGE)
        .order_by(Expense.date, Expense.id)
        .all()
    )
    groups: dict[str, dict] = {}
    unpaid = Decimal("0")
    not_via_bank = Decimal("0")
    for expense in expenses:
        category = normalize_category(expense.category)
        group = groups.setdefault(category, {"name": category, "amount": Decimal("0"), "expenses": []})
        group["amount"] += expense.amount
        group["expenses"].append(expense)
        paid = sum((a.amount for a in expense.allocations), Decimal("0"))
        unpaid += max(expense.amount - paid, Decimal("0"))
        if expense.counterparty.name == CASH_COUNTERPARTY_NAME:
            not_via_bank += expense.amount
    ordered = sorted(groups.values(), key=lambda g: -g["amount"])
    total = sum((g["amount"] for g in ordered), Decimal("0"))
    for g in ordered:
        g["share"] = _percent(g["amount"], total)
    return {"groups": ordered, "total": total, "unpaid": unpaid, "not_via_bank": not_via_bank}


def _budget(year: int) -> dict:
    items = (
        database.db_session.query(BudgetItem)
        .filter(BudgetItem.year == year)
        .order_by(BudgetItem.position, BudgetItem.id)
        .all()
    )
    sections: dict[str, dict] = {}
    for item in items:
        section = sections.setdefault(item.section, {"name": item.section, "amount": Decimal("0"), "lines": []})
        section["amount"] += item.amount
        section["lines"].append({
            "name": item.name, "amount": item.amount, "price": item.price,
            "categories": sorted(_item_categories(item)),
            "qty_text": _fmt_number(item.quantity) if item.quantity is not None and item.price is not None else None,
        })
    ordered = list(sections.values())
    total = sum((s["amount"] for s in ordered), Decimal("0"))
    saved = {
        row.name: row.is_target
        for row in database.db_session.query(BudgetSection).filter(BudgetSection.year == year)
    }
    for s in ordered:
        s["share"] = _percent(s["amount"], total)
        s["kind_saved"] = s["name"] in saved
        s["is_target"] = saved[s["name"]] if s["name"] in saved else guess_section_target(s["name"])
    funding = _section_funding(year, ordered, total)
    for s in ordered:
        s.update(funding["sections"][s["name"]])
    return {"sections": ordered, "total": total, "funding": funding}


_TARGET_SECTION_RE = re.compile(
    r"замен|установк|строительств|ремонт|приобрет|модерниз|реконструк|прокладк|монтаж|видеонаблюд|освещен",
    re.IGNORECASE,
)
# Транзитные виды взноса — собираются не на смету, а чтобы заплатить за
# членов дальше: земельный налог (в ФНС), электричество (поставщику),
# возмещение СМС/госпошлины (telecom_disputes). Пеня — доход, но не
# средства сметы.
TRANSIT_FEE_CODES = {"land_tax", "electricity", "telecom_disputes"}


def guess_section_target(name: str) -> bool:
    """Целевой ли раздел сметы — подсказка по названию («Замена общей
    проводки», «Установка опор» — целевые; «Содержание кооператива»,
    «Взыскание задолженности», «Прочее» — членские). Председатель
    подтверждает или меняет её на странице."""
    return bool(_TARGET_SECTION_RE.search(name or ""))


def _section_funding(year: int, sections: list[dict], total_plan: Decimal) -> dict:
    """
    Сколько собрано на смету года и на каждый её раздел. Средства сметы —
    начисления ЭТОГО года по нетранзитным видам взноса (членский, целевой и
    т.п.; без земельного налога, электричества, возмещений и пени) и
    оплаченная их часть (ChargeAllocation — платежом любой даты, в т.ч. уже
    в следующем году). На разделы делятся ПРОПОРЦИОНАЛЬНО ПЛАНУ раздела в
    смете: человек платит одной суммой, и указать, на какой раздел пошёл
    его рубль, нельзя — только пропорция сметы. Если за год начислялись
    отдельные целевые взносы — это всё равно доли одной сметы.
    """
    charges = (
        database.db_session.query(Charge)
        .options(selectinload(Charge.account).selectinload(MemberAccount.fee_type),
                 selectinload(Charge.allocations).selectinload(ChargeAllocation.payment))
        .filter(Charge.year == year, Charge.account_id.isnot(None))
        .all()
    )
    charged = paid = Decimal("0")
    by_fee: dict[str, dict] = {}
    for charge in charges:
        fee_type = charge.account.fee_type
        if (fee_type is None or fee_type.is_penalty or fee_type.code in TRANSIT_FEE_CODES
                or charge.related_person_id is not None or _is_tech(charge.comment, _TECH_CHARGE_PREFIXES)):
            continue
        charge_paid = sum((
            a.amount for a in charge.allocations
            if a.payment.related_person_id is None and a.payment.offset_charge_id is None
            and a.payment.amnesty_for_charge_id is None and not _is_tech(a.payment.comment, _TECH_PAYMENT_PREFIXES)
        ), Decimal("0"))
        charged += charge.amount
        paid += charge_paid
        row = by_fee.setdefault(fee_type.name, {"name": fee_type.name, "charged": Decimal("0"), "paid": Decimal("0")})
        row["charged"] += charge.amount
        row["paid"] += charge_paid

    per_section = {}
    for section in sections:
        share = section["amount"] / total_plan if total_plan else Decimal("0")
        per_section[section["name"]] = {
            "funded_charged": (charged * share).quantize(Decimal("0.01")),
            "collected": (paid * share).quantize(Decimal("0.01")),
        }
    target_collected = sum((per_section[s["name"]]["collected"] for s in sections if s["is_target"]), Decimal("0"))
    return {
        "charged": charged,
        "paid": paid,
        "fees": sorted(by_fee.values(), key=lambda r: -r["charged"]),
        "sections": per_section,
        "target_collected": target_collected,
        "membership_collected": paid - target_collected if sections else Decimal("0"),
        "rate": _percent(paid, charged),
    }


# Постоянные цвета диаграмм — по смыслу, а не по месту в рейтинге: цвет
# задан у статьи расходов (ExpenseCategory.color) и у вида взноса
# (FeeType.color) в справочнике «Статьи и цвета», один и тот же во всех
# диаграммах и во все годы; совпадающие по смыслу вещи в поступлениях и
# тратах (земельный налог, электроэнергия, пени и штрафы) окрашены
# одинаково. Запасные цвета — для разделов сметы и видов взноса без цвета.
_FALLBACK_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


def _chart_slices(rows: list[dict], key: str, color_of=None) -> list[dict]:
    """Доли для круговой диаграммы с постоянным цветом каждой доли
    (color_of(row) → цвет; по умолчанию — цвет статьи по названию)."""
    rules = category_rules()
    color_of = color_of or (lambda r: rules.colors.get(r["name"], GRAY))
    return [
        {"label": r["name"], "value": float(r[key]), "color": color_of(r)}
        for r in sorted((r for r in rows if r[key] > 0), key=lambda r: -r[key])
    ]


def _income_colors(rows: list[dict]):
    """Цвет вида взноса — из справочника; у вида без цвета — первый
    свободный запасной (не серый у всех сразу)."""
    used = {r["color"] for r in rows if r.get("color")}
    spare = (c for c in _FALLBACK_COLORS if c not in used)
    colors = {r["name"]: r.get("color") or next(spare, GRAY) for r in rows}
    return lambda r: colors[r["name"]]


def _hue_lightness(color: str) -> tuple[float, float]:
    import colorsys
    r, g_, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, lightness, _s = colorsys.rgb_to_hls(r, g_, b)
    return h * 360, lightness


def _too_close(a: str, b: str) -> bool:
    """Два цвета на одной диаграмме не различить: близкий тон (до 25°) и
    близкая светлота («Юридические» малиновый рядом со «штрафами» красным)."""
    (ha, la), (hb, lb) = _hue_lightness(a), _hue_lightness(b)
    hue_gap = min(abs(ha - hb), 360 - abs(ha - hb))
    return hue_gap < 25 and abs(la - lb) < 0.25


def _budget_colors(sections: list[dict]) -> dict[str, str]:
    """Цвет раздела сметы — цвет статьи расходов, на которую приходится
    больше всего денег раздела («Замена проводки» — как «Электрика»); если
    этот цвет уже занят другим разделом — первый свободный из запасных."""
    rules = category_rules()
    colors: dict[str, str] = {}
    used: set[str] = set()
    for section in sections:
        weights: dict[str, Decimal] = {}
        for line in section["lines"]:
            for category in line["categories"]:
                weights[category] = weights.get(category, Decimal("0")) + line["amount"]
        color = None
        if weights:
            best = max(weights, key=weights.get)
            color = rules.colors.get(best)
        if color is None or any(_too_close(color, other) for other in used):
            color = next((c for c in _FALLBACK_COLORS if not any(_too_close(c, other) for other in used)), GRAY)
        used.add(color)
        colors[section["name"]] = color
    return colors


@bp.route("/")
@roles_required(RoleEnum.CHAIRMAN)
def view():
    years = _available_years()
    year = request.args.get("year", type=int)
    if year not in years:
        year = dt.date.today().year

    income = _income(year)
    spending = _spending(year)
    budget = _budget(year)
    counterparties = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    gaps = [suggest_for_line(line, counterparties) for line in unaccounted_debits(year)]
    gaps_total = sum((g["line"].amount for g in gaps), Decimal("0"))

    return render_template(
        "year_report/view.html",
        year=year, years=years,
        income=income, spending=spending, budget=budget,
        gaps=gaps, gaps_total=gaps_total,
        counterparties=counterparties, categories=category_rules().names,
        income_chart=_chart_slices(income["rows"], "received", _income_colors(income["rows"])),
        spending_chart=_chart_slices(spending["groups"], "amount"),
        budget_chart=_chart_slices(budget["sections"], "amount",
                                   lambda r, colors=_budget_colors(budget["sections"]): colors[r["name"]]),
        plan_fact=_with_funding(_plan_fact(year), budget),
        funding_chart=_chart_slices(budget["sections"], "collected",
                                    lambda r, colors=_budget_colors(budget["sections"]): colors[r["name"]]),
        budget_format=csv_format_context("budget"),
        spending_format=csv_format_context("spending"),
    )


def _redirect_back(year: int | None):
    """Обратно на ту же страницу, с которой пришло действие (тот же адрес —
    тогда AJAX-отправка формы, см. base.html: submitFormAjax, подменяет
    только содержимое, без полной перезагрузки). Без JS — к списку
    списаний."""
    referrer = request.referrer or ""
    view_path = url_for("year_report.view")
    if referrer and urlsplit(referrer).path == view_path:
        parts = urlsplit(referrer)
        return redirect(parts.path + (f"?{parts.query}" if parts.query else ""))
    return redirect(url_for("year_report.view", year=year) + "#gaps")


@bp.route("/expenses/<int:expense_id>/category", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def set_expense_category(expense_id):
    """Перенос расхода в другую статью прямо со страницы — если при
    заведении статья подобрана неверно."""
    expense = database.db_session.get(Expense, expense_id)
    if expense is None:
        abort(404)
    category = cap((request.form.get("category") or "").strip(), Expense.__table__.c.category)
    if not category:
        abort(400)
    expense.category = category
    database.db_session.commit()
    return _redirect_back(expense.date.year)


@bp.route("/lines/<int:line_id>/book", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def book_statement_line(line_id):
    line = database.db_session.get(BankStatementLine, line_id)
    if line is None or line.direction != "debit":
        abort(404)
    if line not in available_statement_lines():
        flash(_("Эта строка выписки уже привязана к платежу контрагенту."), "warning")
        return _redirect_back(line.operation_date.year)
    f = request.form
    counterparty = _get_or_create_counterparty(f.get("counterparty_id"), line, f.get("new_name"))
    category = cap(f.get("category") or category_rules().default, Expense.__table__.c.category)
    expense = book_line(line, counterparty, category, f.get("description"))
    audit.record(
        "expense.create", entity_type="counterparty", entity_id=counterparty.id,
        summary=f"Расход {audit.format_amount(expense.amount)} — {counterparty.name}, "
                f"{audit.format_date(expense.date)} (по выписке банка, статья «{category}»)",
    )
    database.db_session.commit()
    flash(_("Расход и платёж заведены."), "success")
    return _redirect_back(line.operation_date.year)


@bp.route("/lines/book-all", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def book_all_statement_lines():
    """Все списания года с уверенной подсказкой статьи — одной кнопкой
    (контрагент — из справочника или новый по названию из выписки)."""
    year = request.form.get("year", type=int)
    counterparties = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    booked = 0
    total = Decimal("0")
    new_names = []
    for suggestion in [suggest_for_line(line, counterparties) for line in unaccounted_debits(year)]:
        if not suggestion["confident"]:
            continue
        line = suggestion["line"]
        counterparty = suggestion["counterparty"]
        if counterparty is None:
            counterparty = _get_or_create_counterparty(None, line, suggestion["new_name"], record=False)
            counterparties.append(counterparty)
            new_names.append(counterparty.name)
        book_line(line, counterparty, suggestion["category"], suggestion["description"], suggestion["date"])
        booked += 1
        total += line.amount
    if booked:
        summary = f"По выписке банка заведено расходов: {booked} на {audit.format_amount(total)} ({year} г.)"
        if new_names:
            summary += "; новые контрагенты: " + ", ".join(f"«{n}»" for n in new_names)
        audit.record("expense.bulk_from_statement", summary=summary)
    database.db_session.commit()
    flash(_("Заведено расходов по выписке: {count}.", count=booked), "success")
    return _redirect_back(year)


@bp.route("/lines/<int:line_id>/not-expense", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def mark_not_expense(line_id):
    line = database.db_session.get(BankStatementLine, line_id)
    if line is None or line.direction != "debit":
        abort(404)
    line.not_expense = True
    audit.record("statement_line.not_expense", entity_type="bank_statement_line", entity_id=line.id,
                 summary=f"Списание {audit.format_amount(line.amount)} от {audit.format_date(line.operation_date)} "
                         f"({line.counterparty_name or '—'}) отмечено как «не трата»")
    database.db_session.commit()
    flash(_("Списание отмечено как «не трата»."), "success")
    return _redirect_back(line.operation_date.year)


@bp.route("/budget/import", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def import_budget():
    year = request.form.get("year", type=int)
    if year is None:
        abort(400)
    text = _decode_upload(request.files.get("file"))
    items = parse_budget_csv(text, get_csv_columns("budget")) if text else []
    if not items:
        flash(_("В файле не найдено ни одной статьи сметы — проверьте формат CSV (какие столбцы в каком порядке)."), "danger")
        return redirect(url_for("year_report.view", year=year))
    # Расходы, уже привязанные к позициям сметы этого года, — после замены
    # сметы привязываются к позиции с тем же разделом и наименованием.
    old_items = database.db_session.query(BudgetItem).filter(BudgetItem.year == year).all()
    links: dict[tuple[str, str], list[Expense]] = {}
    if old_items:
        for expense in database.db_session.query(Expense).filter(
                Expense.budget_item_id.in_([i.id for i in old_items])):
            item = expense.budget_item
            links.setdefault((item.section, item.name), []).append(expense)
            expense.budget_item_id = None
        database.db_session.flush()
    for old in old_items:
        database.db_session.delete(old)
    database.db_session.flush()
    for position, item in enumerate(items):
        new_item = BudgetItem(year=year, position=position, **item)
        database.db_session.add(new_item)
        database.db_session.flush()
        for expense in links.pop((new_item.section, new_item.name), []):
            expense.budget_item_id = new_item.id
    total = sum((i["amount"] for i in items), Decimal("0"))
    audit.record("budget.import", summary=f"Загружена смета на {year} год: {len(items)} статей на {audit.format_amount(total)}")
    database.db_session.commit()
    flash(_("Смета загружена: статей — {count}.", count=len(items)), "success")
    return redirect(url_for("year_report.view", year=year))


# --- Импорт отчёта по тратам ----------------------------------------------

def _match_spending_rows(rows: list[dict]) -> None:
    """
    Подбирает каждой строке отчёта действие по умолчанию (row["action"]):
    - "expense:<id>" — такой расход уже заведён (та же сумма, дата в
      пределах 45 дней) — не заводить повторно, только проставить статью;
    - "line:<id>" — найдено списание в выписке на ту же сумму (оплата от
      10 дней до до 60 дней после даты строки) или на сумму нескольких
      строк одной даты (заказ из нескольких позиций одной оплатой);
    - "cash" — в выписке нет: оплачено не через банк.
    """
    expenses = (
        database.db_session.query(Expense)
        .filter(Expense.kind == ExpenseKind.CHARGE)
        .all()
    )
    used_expenses: set[int] = set()
    lines = [line for line in available_statement_lines() if line.direction == "debit" and not line.not_expense]
    used_lines: set[int] = set()

    # Номер заказа маркетплейса во втором столбце отчёта — точное
    # сопоставление: все позиции заказа идут на ту оплату, в назначении
    # которой этот номер («Оплата по заказу 0161382282-0005»). Сумма
    # позиций может не совпасть с оплатой (скидки, баллы, доставка) — при
    # импорте подгоняется (_fit_to_payment), в предпросмотре видна разница.
    by_order: dict[str, list[dict]] = {}
    for row in rows:
        row["action"] = "cash"
        if row.get("order"):
            by_order.setdefault(row["order"], []).append(row)
    for order, group in by_order.items():
        line = next((l for l in lines if l.id not in used_lines and order in (l.payment_purpose or "")), None)
        if line is None:
            continue
        diff = line.amount - sum((r["amount"] for r in group), Decimal("0"))
        for r in group:
            r["action"] = f"line:{line.id}"
            r["approx_diff"] = diff
        used_lines.add(line.id)

    for row in rows:
        if row["action"] != "cash":
            continue
        for e in expenses:
            if e.id in used_expenses or e.amount != row["amount"]:
                continue
            if abs((e.date - row["date"]).days) <= 45 or abs((e.date - row["paid_on"]).days) <= 45:
                row["action"] = f"expense:{e.id}"
                used_expenses.add(e.id)
                break
        if row["action"] != "cash":
            continue
        for line in lines:
            if line.id in used_lines or line.amount != row["amount"]:
                continue
            if -10 <= (line.operation_date - row["paid_on"]).days <= 60:
                row["action"] = f"line:{line.id}"
                used_lines.add(line.id)
                break

    # Несколько позиций одной даты одной оплатой.
    by_date: dict[dt.date, list[dict]] = {}
    for row in rows:
        if row["action"] == "cash":
            by_date.setdefault(row["paid_on"], []).append(row)
    for paid_on, group in by_date.items():
        if len(group) < 2:
            continue
        group_sum = sum((r["amount"] for r in group), Decimal("0"))
        for line in lines:
            if line.id in used_lines or line.amount != group_sum:
                continue
            if -10 <= (line.operation_date - paid_on).days <= 60:
                for r in group:
                    r["action"] = f"line:{line.id}"
                used_lines.add(line.id)
                break

    # Заказ, оформленный в разные дни (позиции отчёта датированы днём
    # покупки, оплата — одна): ищем комбинацию ещё не сопоставленных строк
    # за 60 дней до оплаты (и 10 после), дающую сумму оплаты точно.
    for line in lines:
        if line.id in used_lines:
            continue
        candidates = [
            r for r in rows
            if r["action"] == "cash" and -10 <= (line.operation_date - r["paid_on"]).days <= 60
        ][:18]
        subset = _exact_subset(candidates, line.amount)
        if subset:
            for r in subset:
                r["action"] = f"line:{line.id}"
            used_lines.add(line.id)


def _fit_to_payment(rows: list[dict], paid: Decimal) -> list[Decimal]:
    """Суммы позиций, пропорционально подогнанные под фактическую оплату
    (копеечный остаток округления — на самую крупную позицию)."""
    listed = sum((r["amount"] for r in rows), Decimal("0"))
    if not listed or listed == paid:
        return [r["amount"] for r in rows]
    amounts = [(r["amount"] * paid / listed).quantize(Decimal("0.01")) for r in rows]
    biggest = max(range(len(rows)), key=lambda i: rows[i]["amount"])
    amounts[biggest] += paid - sum(amounts, Decimal("0"))
    return amounts


def _exact_subset(rows: list[dict], target: Decimal) -> list[dict] | None:
    """Подмножество строк (2 и больше) с суммой ровно target — перебор
    сумм по копейкам, без повторов; None, если такого нет."""
    goal = int(target * 100)
    sums: dict[int, tuple[int, ...]] = {0: ()}
    for i, row in enumerate(rows):
        cents = int(row["amount"] * 100)
        for s, chosen in list(sums.items()):
            total = s + cents
            if total <= goal and total not in sums:
                sums[total] = chosen + (i,)
        if goal in sums and len(sums[goal]) >= 2:
            return [rows[j] for j in sums[goal]]
        if len(sums) > 300_000:
            break
    return None


@bp.route("/spending/import", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def import_spending_preview():
    year = request.form.get("year", type=int)
    text = _decode_upload(request.files.get("file"))
    rows = parse_spending_csv(text, get_csv_columns("spending")) if text else []
    if not rows:
        flash(_("В файле не найдено ни одной траты — проверьте формат CSV (какие столбцы в каком порядке)."), "danger")
        return redirect(url_for("year_report.view", year=year))
    _match_spending_rows(rows)
    line_ids = {int(r["action"].split(":")[1]) for r in rows if r["action"].startswith("line:")}
    expense_ids = {int(r["action"].split(":")[1]) for r in rows if r["action"].startswith("expense:")}
    lines = {line.id: line for line in database.db_session.query(BankStatementLine).filter(BankStatementLine.id.in_(line_ids))} if line_ids else {}
    expenses = {e.id: e for e in database.db_session.query(Expense).filter(Expense.id.in_(expense_ids))} if expense_ids else {}
    # Для ручной привязки — все ещё свободные списания (выписка может не
    # совпасть с отчётом копейка в копейку: скидка на маркетплейсе и т.п.).
    free_lines = [line for line in unaccounted_debits() if abs(line.operation_date.year - (year or line.operation_date.year)) <= 1]
    # И уже заведённые расходы рядом по дате — строку отчёта можно отнести к
    # одному из них («уже учтено»: Русский Свет, оплаченный одним счётом на
    # другую сумму, чем позиции в отчёте).
    all_expenses = (
        database.db_session.query(Expense)
        .options(selectinload(Expense.counterparty))
        .filter(Expense.kind == ExpenseKind.CHARGE)
        .all()
    )
    for row in rows:
        near = [e for e in all_expenses if abs((e.date - row["date"]).days) <= 60]
        near.sort(key=lambda e: (abs((e.date - row["date"]).days), e.id))
        row["nearby_expenses"] = near[:6]
        # Подсказка для строки «не через банк»: рядом (±7 дней) уже есть
        # расход той же статьи — вероятно, позиция оплачена им (счёт
        # поставщика одной суммой на весь заказ).
        row["hint_expense"] = next(
            (e for e in near if abs((e.date - row["date"]).days) <= 7
             and normalize_category(e.category) == row["category"]), None,
        ) if row["action"] == "cash" else None
        if row["action"].startswith("expense:"):
            current = int(row["action"].split(":")[1])
            if current not in {e.id for e in row["nearby_expenses"]} and current in expenses:
                row["nearby_expenses"].insert(0, expenses[current])
    return render_template(
        "year_report/import_spending.html",
        year=year, rows=rows, csv_text=text, lines=lines, expenses=expenses,
        free_lines=free_lines, categories=category_rules().names,
        total=sum((r["amount"] for r in rows), Decimal("0")),
    )


@bp.route("/spending/import/apply", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def import_spending_apply():
    f = request.form
    year = f.get("year", type=int)
    rows = parse_spending_csv(f.get("csv_text") or "", get_csv_columns("spending"))
    by_index = {r["index"]: r for r in rows}
    counterparties = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    available = {line.id: line for line in available_statement_lines()}

    to_line: dict[int, list[dict]] = {}
    new_names: list[str] = []
    created = updated = 0
    total = Decimal("0")
    touched: dict[int, Counterparty] = {}
    for index, row in by_index.items():
        action = f.get(f"action_{index}") or "skip"
        category = cap(f.get(f"category_{index}") or row["category"], Expense.__table__.c.category)
        if action == "skip":
            continue
        if action.startswith("expense:"):
            expense = database.db_session.get(Expense, int(action.split(":")[1]))
            if expense is not None and expense.category != category:
                expense.category = category
                updated += 1
            continue
        row["category"] = category
        if action.startswith("line:"):
            line_id = int(action.split(":")[1])
            if line_id in available:
                to_line.setdefault(line_id, []).append(row)
                continue
            action = "cash"
        if action == "cash":
            cp = _cash_counterparty()
            database.db_session.add(Expense(
                counterparty_id=cp.id, date=row["date"], amount=row["amount"],
                category=category, description=row["description"], kind=ExpenseKind.CHARGE,
            ))
            database.db_session.flush()
            pay_counterparty(cp, row["paid_on"], row["amount"], comment=_("Оплачено не через банк"), adjust_balance=False)
            created += 1
            total += row["amount"]

    for line_id, line_rows in to_line.items():
        line = available[line_id]
        counterparty = match_counterparty(line, counterparties)
        if counterparty is None:
            counterparty = _get_or_create_counterparty(None, line, None, record=False)
            counterparties.append(counterparty)
            new_names.append(counterparty.name)
        # Сумма позиций не сошлась с оплатой (скидка, баллы, доставка;
        # строка вручную привязана к оплате на другую сумму) — расходы
        # подгоняются под реально списанное, цена из отчёта остаётся в
        # описании.
        fitted = _fit_to_payment(line_rows, line.amount)
        for row, amount in zip(line_rows, fitted):
            description = row["description"]
            if amount != row["amount"]:
                description = f"{description} — " + _("по отчёту {amount} ₽", amount=_fmt_money(row["amount"]))
            database.db_session.add(Expense(
                counterparty_id=counterparty.id, date=row["date"], amount=amount,
                category=row["category"], description=description, kind=ExpenseKind.CHARGE,
            ))
            created += 1
            total += amount
        database.db_session.flush()
        pay_counterparty(
            counterparty, line.operation_date, line.amount, bank_account=line.bank_account,
            comment=(line.payment_purpose or "").strip()[:500] or None, adjust_balance=False,
            bank_statement_line_id=line.id,
        )
        touched[counterparty.id] = counterparty
    for counterparty in touched.values():
        reallocate_counterparty_expenses(counterparty)

    summary = (f"Импорт отчёта по тратам за {year} г.: заведено расходов {created} на "
               f"{audit.format_amount(total)}, уточнена статья у {updated}")
    if new_names:
        summary += "; новые контрагенты: " + ", ".join(f"«{n}»" for n in new_names)
    audit.record("expense.import_spending_report", summary=summary)
    database.db_session.commit()
    flash(_("Отчёт по тратам импортирован: заведено расходов — {created}, уточнена статья — {updated}.",
            created=created, updated=updated), "success")
    return redirect(url_for("year_report.view", year=year))


# --- Формат CSV ------------------------------------------------------------

def csv_format_context(kind: str) -> dict:
    """Параметры общей модалки setup/_csv_format_modal.html."""
    _import_type, catalog = CSV_FORMATS[kind]
    columns = get_csv_columns(kind)
    positions = {key: i + 1 for i, key in enumerate(columns) if key}
    labels = dict((k, lbl) for k, lbl, _r in catalog)
    return {
        "modal_id": f"{kind}FormatModal",
        "format_action": url_for("year_report.save_csv_format", kind=kind),
        "catalog": catalog,
        "active_keys": set(positions),
        "positions": positions,
        "summary": [(i + 1, labels[key]) for i, key in enumerate(columns) if key],
    }


@bp.route("/format/<kind>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_csv_format(kind):
    if kind not in CSV_FORMATS:
        abort(404)
    import_type, catalog = CSV_FORMATS[kind]
    positions: dict[int, str] = {}
    for key, _label, required in catalog:
        if not request.form.get(f"col_{key}"):
            continue
        try:
            pos = int(request.form.get(f"pos_{key}") or 0)
        except ValueError:
            pos = 0
        if pos < 1:
            flash(_("Укажите номер столбца для каждой отмеченной колонки (начиная с 1)."), "danger")
            return _redirect_back(None)
        if pos in positions:
            flash(_("Две колонки не могут занимать один и тот же столбец файла ({pos}).", pos=pos), "danger")
            return _redirect_back(None)
        positions[pos] = key
    required = {k for k, _l, r in catalog if r}
    if not required.issubset(positions.values()):
        flash(_("В формате обязательно должны быть колонки «Наименование», «Сумма» (и «Дата» — для отчёта по тратам)."), "danger")
        return _redirect_back(None)
    columns = [positions.get(i, "") for i in range(1, max(positions) + 1)]
    payload = json.dumps(columns, ensure_ascii=False)
    profile = database.db_session.query(CsvImportProfile).filter_by(import_type=import_type).first()
    if profile is None:
        database.db_session.add(CsvImportProfile(import_type=import_type, columns=payload))
    else:
        profile.columns = payload
    database.db_session.commit()
    flash(_("Формат CSV сохранён."), "success")
    return _redirect_back(None)


@bp.route("/format/<kind>/template.csv")
@roles_required(RoleEnum.CHAIRMAN)
def csv_template(kind):
    if kind not in CSV_FORMATS:
        abort(404)
    columns = get_csv_columns(kind)
    buf = io.StringIO()
    writer = csv.writer(buf)
    for example in _EXAMPLE_ROWS[kind]:
        writer.writerow([example.get(key, "") if key else "" for key in columns])
    filename = "budget_template.csv" if kind == "budget" else "spending_template.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# --- Смета и факт ---------------------------------------------------------


_MATCH_STOP_STEMS = {"оплат", "счету", "числе", "рубле", "товар", "услуг", "работ", "резер", "проче", "налог",
                     "общий", "счетч"}


def _stems(text: str | None) -> set[str]:
    words = re.findall(r"[а-яa-z]{4,}", (text or "").lower().replace("ё", "е"))
    return {w[:5] for w in words} - _MATCH_STOP_STEMS


def _item_categories(item: BudgetItem) -> set[str]:
    """Все статьи расходов, на которые похожа позиция сметы («Резерв на
    оплату электроэнергии (+ штрафы…)» — и электроэнергия, и штрафы)."""
    return category_rules().all_matching(f"{item.name} {item.section}")


def suggest_budget_item(expense: Expense, items: list[BudgetItem], item_cats: dict[int, set[str]]) -> BudgetItem | None:
    """Позиция сметы для расхода: больше всего общих слов (по первым 5
    буквам) в описании/контрагенте и наименовании позиции, плюс балл за
    совпадение статьи; нужно хотя бы 2 балла. Иначе — для узких статей
    (ExpenseCategory.budget_match_by_category), если статье расхода в смете соответствует ровно
    одна позиция (комиссии банка → «платёжные поручения»), она."""
    category = normalize_category(expense.category)
    expense_stems = _stems(" ".join(filter(None, [expense.description, expense.counterparty.name])))
    best, best_score = None, 0
    for item in items:
        score = len(expense_stems & _stems(item.name)) + (1 if category in item_cats[item.id] else 0)
        if score > best_score:
            best, best_score = item, score
    if best_score >= 2:
        return best
    # Только для узких статей: комиссия банка — это и есть «платёжные
    # поручения», а вот единственная в смете позиция хоз. работ («уборка
    # снега») ещё не значит, что подсыпка дороги заложена в смету.
    if category not in category_rules().narrow:
        return None
    same = [item for item in items if category in item_cats[item.id]]
    return same[0] if len(same) == 1 else None


def _plan_fact(year: int) -> dict:
    """
    Смета против факта за год. Каждый расход года (kind=CHARGE):
    - привязан к позиции сметы явно (budget_item_id) или подобран
      автоматически (suggested=True — показывается со значком «≈», можно
      закрепить или поменять);
    - вне сметы — нецелевые траты (явно отмечен off_budget или подбор не
      нашёл позицию);
    - земельный налог — отдельно: собирается отдельным взносом и уходит в
      ФНС транзитом, в смету не входит (если явно не привязан к позиции).
    """
    items = (
        database.db_session.query(BudgetItem)
        .filter(BudgetItem.year == year)
        .order_by(BudgetItem.position, BudgetItem.id)
        .all()
    )
    item_cats = {item.id: _item_categories(item) for item in items}
    outside_names = category_rules().outside
    by_item: dict[int, dict] = {
        item.id: {"item": item, "plan": item.amount, "fact": Decimal("0"), "expenses": []} for item in items
    }
    expenses = (
        database.db_session.query(Expense)
        .options(selectinload(Expense.counterparty))
        .filter(Expense.date >= dt.date(year, 1, 1), Expense.date <= dt.date(year, 12, 31),
                Expense.kind == ExpenseKind.CHARGE)
        .order_by(Expense.date, Expense.id)
        .all()
    )
    off: dict[str, dict] = {}
    land = {"total": Decimal("0"), "expenses": []}
    suggested_count = 0
    for expense in expenses:
        target = None
        suggested = False
        if expense.budget_item_id in by_item:
            target = by_item[expense.budget_item_id]
        elif not expense.off_budget and items and normalize_category(expense.category) not in outside_names:
            match = suggest_budget_item(expense, items, item_cats)
            if match is not None:
                target, suggested = by_item[match.id], True
                suggested_count += 1
        if target is not None:
            target["fact"] += expense.amount
            target["expenses"].append({"expense": expense, "suggested": suggested})
        elif normalize_category(expense.category) in outside_names and not expense.off_budget:
            land["total"] += expense.amount
            land["expenses"].append(expense)
        else:
            category = normalize_category(expense.category)
            group = off.setdefault(category, {"name": category, "total": Decimal("0"), "expenses": []})
            group["total"] += expense.amount
            group["expenses"].append(expense)

    sections: list[dict] = []
    for row in by_item.values():
        section_name = row["item"].section
        if not sections or sections[-1]["name"] != section_name:
            sections.append({"name": section_name, "plan": Decimal("0"), "fact": Decimal("0"), "items": []})
        sections[-1]["plan"] += row["plan"]
        sections[-1]["fact"] += row["fact"]
        sections[-1]["items"].append(row)
    for block in [*sections, *[r for sec in sections for r in sec["items"]]]:
        block["meter"] = _meter(block["plan"], block["fact"])
    plan = sum((sec["plan"] for sec in sections), Decimal("0"))
    fact = sum((sec["fact"] for sec in sections), Decimal("0"))
    off_groups = sorted(off.values(), key=lambda g: -g["total"])
    off_total = sum((g["total"] for g in off_groups), Decimal("0"))
    return {
        "items": items,
        "sections": sections,
        "plan": plan,
        "fact": fact,
        "meter": _meter(plan, fact),
        "unspent": max(plan - fact, Decimal("0")),
        "off_groups": off_groups,
        "off_total": off_total,
        "off_count": sum(len(g["expenses"]) for g in off_groups),
        "land": land,
        "outside_label": ", ".join(name for name in category_rules().names if name in category_rules().outside),
        "suggested_count": suggested_count,
        "in_budget_share": _percent(fact, fact + off_total),
    }


def _meter(plan: Decimal, fact: Decimal) -> dict:
    """Полоса «потрачено из запланированного»: шкала — большее из плана и
    факта; в пределах плана — синяя часть, сверх плана — красная, отметка
    плана — тонкая черта."""
    scale = max(plan, fact)
    if not scale:
        return {"within": 0, "over": 0, "plan_at": 0, "rate": None, "over_amount": Decimal("0")}
    within = min(plan, fact)
    return {
        "within": float(within / scale * 100),
        "over": float(max(fact - plan, Decimal("0")) / scale * 100),
        "plan_at": float(plan / scale * 100),
        "rate": _percent(fact, plan),
        "over_amount": max(fact - plan, Decimal("0")),
    }


@bp.route("/expenses/<int:expense_id>/budget", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def set_expense_budget_item(expense_id):
    """Позиция сметы для расхода: id позиции, «off» — вне сметы, «auto» —
    снова подбирать автоматически."""
    expense = database.db_session.get(Expense, expense_id)
    if expense is None:
        abort(404)
    value = request.form.get("budget_item") or "auto"
    if value == "off":
        expense.budget_item_id, expense.off_budget = None, True
    elif value == "auto":
        expense.budget_item_id, expense.off_budget = None, False
    else:
        item = database.db_session.get(BudgetItem, int(value))
        if item is None:
            abort(400)
        expense.budget_item_id, expense.off_budget = item.id, False
    database.db_session.commit()
    return _redirect_back(expense.date.year)


@bp.route("/plan-fact/confirm", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def confirm_budget_suggestions():
    """Закрепить автоматический подбор позиций сметы за год — дальше
    привязка не меняется при правке описаний или сметы."""
    year = request.form.get("year", type=int)
    if year is None:
        abort(400)
    plan_fact = _plan_fact(year)
    confirmed = 0
    for section in plan_fact["sections"]:
        for row in section["items"]:
            for entry in row["expenses"]:
                if entry["suggested"]:
                    entry["expense"].budget_item_id = row["item"].id
                    confirmed += 1
    database.db_session.commit()
    flash(_("Подбор закреплён: расходов — {count}.", count=confirmed), "success")
    return _redirect_back(year)


# --- Справочник статей и цветов ------------------------------------------------

_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _category_form_values(f) -> dict:
    color = (f.get("color") or "").strip()
    return {
        "name": cap((f.get("name") or "").strip(), ExpenseCategory.__table__.c.name),
        "color": color.lower() if _COLOR_RE.fullmatch(color) else None,
        "keywords": (f.get("keywords") or "").strip() or None,
        "position": f.get("position", type=int) or 0,
        "outside_budget": bool(f.get("outside_budget")),
        "budget_match_by_category": bool(f.get("budget_match_by_category")),
    }


def _ensure_categories_seeded() -> None:
    """Справочник пуст (новая установка до миграции с данными или всё
    удалено) — заносим встроенные статьи, чтобы было что править."""
    if database.db_session.query(ExpenseCategory).count():
        return
    for i, (name, color, keywords, outside, by_category, default) in enumerate(DEFAULT_CATEGORIES):
        database.db_session.add(ExpenseCategory(
            name=name, color=color, keywords=keywords, position=i * 10, outside_budget=outside,
            budget_match_by_category=by_category, is_default=default,
        ))
    database.db_session.commit()


@bp.route("/categories")
@roles_required(RoleEnum.CHAIRMAN)
def categories_view():
    _ensure_categories_seeded()
    categories = database.db_session.query(ExpenseCategory).order_by(ExpenseCategory.position, ExpenseCategory.id).all()
    usage: dict[str, int] = {}
    for (raw,) in database.db_session.query(Expense.category):
        name = normalize_category(raw)
        usage[name] = usage.get(name, 0) + 1
    fee_types = database.db_session.query(FeeType).order_by(FeeType.is_penalty, FeeType.name).all()
    return render_template(
        "year_report/categories.html", categories=categories, usage=usage, fee_types=fee_types,
        next_position=(max((c.position for c in categories), default=0) + 10),
        year=request.args.get("year", type=int),
    )


def _back_to_categories():
    # Справочник изменился — подбор статей в этом же контексте должен
    # видеть новое (кэш category_rules живёт на g).
    g.pop("_expense_category_rules", None)
    return redirect(request.referrer if request.referrer and urlsplit(request.referrer).path == url_for(
        "year_report.categories_view") else url_for("year_report.categories_view"))


@bp.route("/categories/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create_category():
    values = _category_form_values(request.form)
    if not values["name"]:
        flash(_("Укажите название статьи."), "danger")
    elif category_rules().exact(values["name"]) or database.db_session.query(ExpenseCategory).filter(
            ExpenseCategory.name == values["name"]).first():
        flash(_("Статья «{name}» уже есть.", name=values["name"]), "danger")
    else:
        database.db_session.add(ExpenseCategory(**values))
        audit.record("expense_category.create", summary=f"Добавлена статья расходов «{values['name']}»")
        database.db_session.commit()
        flash(_("Статья добавлена."), "success")
    return _back_to_categories()


@bp.route("/categories/<int:category_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit_category(category_id):
    category = database.db_session.get(ExpenseCategory, category_id)
    if category is None:
        abort(404)
    values = _category_form_values(request.form)
    if not values["name"]:
        flash(_("Укажите название статьи."), "danger")
        return _back_to_categories()
    old_name = category.name
    if values["name"] != old_name and database.db_session.query(ExpenseCategory).filter(
            ExpenseCategory.name == values["name"], ExpenseCategory.id != category.id).first():
        flash(_("Статья «{name}» уже есть.", name=values["name"]), "danger")
        return _back_to_categories()
    for key, value in values.items():
        setattr(category, key, value)
    if values["name"] != old_name:
        # Расходы хранят название статьи — переименовываем и их.
        moved = database.db_session.query(Expense).filter(Expense.category == old_name).update(
            {"category": values["name"]}, synchronize_session=False)
        audit.record("expense_category.rename",
                     summary=f"Статья расходов «{old_name}» переименована в «{values['name']}» (расходов: {moved})")
    database.db_session.commit()
    flash(_("Статья «{name}» сохранена.", name=category.name), "success")
    return _back_to_categories()


@bp.route("/categories/<int:category_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_category(category_id):
    category = database.db_session.get(ExpenseCategory, category_id)
    if category is None:
        abort(404)
    if category.is_default:
        flash(_("Статью по умолчанию удалить нельзя — в неё попадает всё неопознанное."), "danger")
        return _back_to_categories()
    default = category_rules().default
    moved = database.db_session.query(Expense).filter(Expense.category == category.name).update(
        {"category": default}, synchronize_session=False)
    audit.record("expense_category.delete",
                 summary=f"Удалена статья расходов «{category.name}», её расходы ({moved}) перенесены в «{default}»")
    database.db_session.delete(category)
    database.db_session.commit()
    flash(_("Статья удалена, её расходы перенесены в «{name}».", name=default), "success")
    return _back_to_categories()


@bp.route("/fee-types/<int:fee_type_id>/color", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def set_fee_type_color(fee_type_id):
    fee_type = database.db_session.get(FeeType, fee_type_id)
    if fee_type is None:
        abort(404)
    color = (request.form.get("color") or "").strip()
    fee_type.color = color.lower() if _COLOR_RE.fullmatch(color) else None
    database.db_session.commit()
    flash(_("Цвет «{name}» сохранён.", name=fee_type.name), "success")
    return _back_to_categories()


def _with_funding(plan_fact: dict, budget: dict) -> dict:
    """Добавить к разделам «Сметы и факта» собранное на раздел (доля
    средств сметы по плану) и остаток: собрано − потрачено (плюс —
    переходит на следующий год, минус — дефицит раздела)."""
    by_name = {s["name"]: s for s in budget["sections"]}
    for section in plan_fact["sections"]:
        info = by_name.get(section["name"], {})
        section["is_target"] = info.get("is_target", False)
        section["collected"] = info.get("collected", Decimal("0"))
        section["balance"] = section["collected"] - section["fact"]
    plan_fact["collected"] = budget["funding"]["paid"]
    plan_fact["balance"] = plan_fact["collected"] - plan_fact["fact"]
    return plan_fact


@bp.route("/budget/section-kind", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def set_section_kind():
    """Членский или целевой раздел сметы."""
    year = request.form.get("year", type=int)
    name = cap((request.form.get("section") or "").strip(), BudgetSection.__table__.c.name)
    if year is None or not name:
        abort(400)
    row = database.db_session.query(BudgetSection).filter_by(year=year, name=name).first()
    if row is None:
        row = BudgetSection(year=year, name=name)
        database.db_session.add(row)
    row.is_target = request.form.get("kind") == "target"
    database.db_session.commit()
    return _redirect_back(year)
