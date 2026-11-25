"""Подстановки живых данных в текст страниц вики: {{тариф_членам}},
{{дата_тарифа_поставщика}}, {{поставщик}} … — значения берутся из базы
при каждом показе страницы, поэтому статья не устаревает, когда правление
вносит новый тариф или меняет поставщика в настройках «Электроэнергии».

Кроме отдельных значений — простая арифметика для примеров расчёта:
{{100 * тариф_членам}}, {{тариф_поставщика * (1 + потери_в_сети / 100)}}.
Разрешены только числа (десятичная запятая или точка), числовые
подстановки, + − × / и скобки — разбор через ast, без eval. Результат
выражения — сумма с двумя знаками после запятой («672,65»).

Тот же синтаксис {{…}}, что у шаблонов документов (app/documents.py), и
те же реквизиты кооператива (группа «Кооператив» оттуда). Неизвестный
ключ или выражение с неизвестным именем остаются в тексте как есть (видно
опечатку; и конфиги устройств с {{…}} в теле не ломаются), внутри `кода`
и блоков ``` подстановок нет.
"""
import ast
import datetime as dt
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from . import database
from .models import ElectricitySettings, ElectricityTariff, ElectricityTariffKind

PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}<>\n]+?)\s*\}\}")
# Код в markdown: блоки ``` … ``` и `инлайн` — подстановки внутри не трогаем.
_CODE_RE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
MISSING = "—"


def _number(value: Decimal) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _rate(value: Decimal) -> str:
    """Тариф: не меньше двух знаков после запятой («6,50»), но без потери
    точности, если ставка задана точнее («6,4468»)."""
    text = _number(value)
    whole, _, frac = text.partition(",")
    return f"{whole},{frac.ljust(2, '0')}"


def _date(value: dt.date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


class Context:
    """Данные, общие для всех подстановок одной страницы (одна выборка из
    базы на страницу, а не на каждое вхождение)."""

    def __init__(self, today: dt.date | None = None):
        self.today = today or dt.date.today()
        self._cache: dict = {}

    def _get(self, name, fn):
        if name not in self._cache:
            self._cache[name] = fn()
        return self._cache[name]

    def tariff(self, kind: ElectricityTariffKind) -> ElectricityTariff | None:
        return self._get(("tariff", kind), lambda: (
            database.db_session.query(ElectricityTariff)
            .filter(ElectricityTariff.kind == kind, ElectricityTariff.effective_date <= self.today)
            .order_by(ElectricityTariff.effective_date.desc(), ElectricityTariff.id.desc())
            .first()
        ))

    def next_tariff(self, kind: ElectricityTariffKind) -> ElectricityTariff | None:
        """Уже внесённый, но ещё не вступивший в силу тариф."""
        return self._get(("next", kind), lambda: (
            database.db_session.query(ElectricityTariff)
            .filter(ElectricityTariff.kind == kind, ElectricityTariff.effective_date > self.today)
            .order_by(ElectricityTariff.effective_date, ElectricityTariff.id)
            .first()
        ))

    @property
    def settings(self) -> ElectricitySettings | None:
        # Только чтение: get_electricity_settings() создала бы запись при показе страницы.
        return self._get("settings", lambda: database.db_session.query(ElectricitySettings).first())

    @property
    def supplier(self):
        return self.settings.supplier if self.settings else None

    @property
    def documents_context(self):
        from . import documents
        return self._get("documents", lambda: documents.Context(self.today))

    def supplier_attr(self, name: str) -> str:
        return (getattr(self.supplier, name, None) or "") if self.supplier else ""


def _rate_of(kind, attr="tariff"):
    return lambda c: (t.rate if (t := getattr(c, attr)(kind)) else None)


def _date_of(kind, attr="tariff"):
    return lambda c: (_date(t.effective_date) if (t := getattr(c, attr)(kind)) else "")


def _supplier_contract(c) -> str:
    cp = c.supplier
    if cp is None or not (cp.contract_number or cp.contract_date):
        return ""
    parts = [f"№ {cp.contract_number}"] if cp.contract_number else []
    if cp.contract_date:
        parts.append(f"от {_date(cp.contract_date)}")
    return " ".join(parts)


_M, _S = ElectricityTariffKind.MEMBER, ElectricityTariffKind.SUPPLIER

# (ключ, описание, функция(ctx) -> Decimal | str | None, формат числа).
# Числовые (формат не None) годятся и в выражения.
VARIABLES = [
    ("тариф_членам", "действующий тариф для членов кооператива, ₽/кВт·ч", _rate_of(_M), _rate),
    ("дата_тарифа_членам", "с какой даты действует тариф для членов", _date_of(_M), None),
    ("тариф_поставщика", "действующий тариф поставщика (энергосбыта), ₽/кВт·ч", _rate_of(_S), _rate),
    ("дата_тарифа_поставщика", "с какой даты действует тариф поставщика", _date_of(_S), None),
    ("новый_тариф_членам", "внесённый, но ещё не вступивший в силу тариф для членов (пусто, если нет)",
     _rate_of(_M, "next_tariff"), _rate),
    ("дата_нового_тарифа_членам", "дата вступления нового тарифа для членов", _date_of(_M, "next_tariff"), None),
    ("новый_тариф_поставщика", "внесённый, но ещё не вступивший в силу тариф поставщика",
     _rate_of(_S, "next_tariff"), _rate),
    ("дата_нового_тарифа_поставщика", "дата вступления нового тарифа поставщика",
     _date_of(_S, "next_tariff"), None),
    ("потери_в_сети", "потери в сетях до кооператива, % (настройки «Электроэнергии»)",
     lambda c: c.settings.outside_line_loss_percent if c.settings else None, _number),
    ("самопотребление_счётчика", "собственное потребление счётчика, Вт",
     lambda c: c.settings.meter_self_consumption_w if c.settings else None, _number),
    ("поставщик", "поставщик электроэнергии — наименование контрагента", lambda c: c.supplier_attr("name"), None),
    ("ИНН_поставщика", "ИНН поставщика", lambda c: c.supplier_attr("inn"), None),
    ("телефон_поставщика", "телефоны поставщика", lambda c: c.supplier_attr("phone"), None),
    ("email_поставщика", "электронная почта поставщика", lambda c: c.supplier_attr("email"), None),
    ("адрес_поставщика", "адрес поставщика", lambda c: c.supplier_attr("address"), None),
    ("договор_поставщика", "договор с поставщиком: «№ 15-Э от 01.02.2025»", _supplier_contract, None),
]
_BY_KEY = {key.lower(): (key, fn, fmt) for key, _desc, fn, fmt in VARIABLES}


def _coop_variables():
    from . import documents
    return [(key, desc, fn) for key, desc, group, fn in documents.VARIABLES if group == "Кооператив"]


def _lookup(name: str, ctx: Context):
    """(значение, формат) или None, если имя неизвестно."""
    found = _BY_KEY.get(name.lower())
    if found is not None:
        _key, fn, fmt = found
        return fn(ctx), fmt
    for key, _desc, fn in _coop_variables():
        if key.lower() == name.lower():
            return fn(ctx.documents_context), None
    return None


class _Unknown(Exception):
    pass


class _NoValue(Exception):
    pass


_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
           ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}


def _eval(node, ctx: Context) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval(node.left, ctx), _eval(node.right, ctx))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval(node.operand, ctx)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return Decimal(str(node.value))
    if isinstance(node, ast.Name):
        found = _lookup(node.id, ctx)
        if found is None or found[1] is None:  # неизвестное или нечисловое имя
            raise _Unknown(node.id)
        if found[0] is None:
            raise _NoValue(node.id)
        return Decimal(found[0])
    raise _Unknown(type(node).__name__)


def _render_one(raw: str, ctx: Context) -> str | None:
    """Текст подстановки или None — оставить {{…}} как есть."""
    raw = raw.strip()
    found = _lookup(raw, ctx)
    if found is not None:
        value, fmt = found
        if value is None or value == "":
            return MISSING
        return fmt(Decimal(value)) if fmt else " ".join(str(value).split())
    if len(raw) > 200:
        return None
    try:
        tree = ast.parse(_DECIMAL_COMMA_RE.sub(".", raw), mode="eval")
        value = _eval(tree, ctx)
    except (SyntaxError, ValueError, _Unknown):
        return None
    except (_NoValue, ZeroDivisionError, InvalidOperation):
        return MISSING
    return _money(value.quantize(Decimal("0.01"), ROUND_HALF_UP))


def substitute(text: str, ctx: Context | None = None) -> str:
    """Подставить значения в markdown страницы вики (вне кода)."""
    if not text or "{{" not in text:
        return text or ""
    ctx = ctx or Context()

    def fill(chunk: str) -> str:
        def replace(match: re.Match) -> str:
            value = _render_one(match.group(1), ctx)
            return match.group(0) if value is None else value
        return PLACEHOLDER_RE.sub(replace, chunk)

    out, pos = [], 0
    for code in _CODE_RE.finditer(text):
        out.append(fill(text[pos:code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(fill(text[pos:]))
    return "".join(out)


def variable_list(ctx: Context | None = None) -> list[tuple[str, str, str]]:
    """(ключ, описание, текущее значение) — подсказка в форме страницы."""
    ctx = ctx or Context()
    rows = [(key, desc, _render_one(key, ctx)) for key, desc, _fn, _fmt in VARIABLES]
    rows += [(key, desc, _render_one(key, ctx)) for key, desc, _fn in _coop_variables()]
    return rows
