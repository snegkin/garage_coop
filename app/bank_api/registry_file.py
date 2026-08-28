"""
Текстовый формат файла реестра начислений/платежей для СберБизнес Онлайн
(«приём платежей от физических лиц» — тот функционал, где плательщик видит
начисление по лицевому счёту в Сбербанк Онлайн и платит без реквизитов
получателя). Это отдельный, более старый канал обмена, чем современный
Fintech API (RKO — расчётные операции, выписки, платёжные поручения): для
начислений/платежей физлиц банк исторически принимает и отдаёт именно
текстовый файл в кодировке **Windows-1251 (cp1251)**, а не JSON.

**Порядок и состав полей — настраиваемый (RegistryFormat), а не зашитый.**
На практике он различается по конкретному договору/подключению кооператива
(разные банки и даже разные подключения к одному банку присылают/ждут поля
в разном порядке, с разным набором необязательных колонок) — так же, как
формат CSV-импорта людей/гаражей в мастере настройки (app/setup_wizard.py,
тот же принцип: каталог полей + чекбокс/позиция в UI, а не жёсткий парсинг
по номеру колонки). Значения по умолчанию ниже (DEFAULT_FORMAT) — это
РЕАЛЬНЫЙ формат, полученный от Sne по образцам файлов реестра начислений
(`account_number;payer_name;purpose;service_code;amount`, разделитель
дробной части — точка) и реестра платежей (13 полей, разделитель дробной
части — запятая, с итоговой строкой `=count;total;creditedTotal;feeTotal;
batchNumber;date`, которая явно пропускается при разборе как не строка
данных, а сводка) — НЕ придуманы умозрительно, как было в первой версии
этого модуля.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal, InvalidOperation

from .base import ChargeRegistryItem, PaymentRegistryItem

ENCODING = "cp1251"
LINE_ENDING = "\r\n"  # DOS-конец строки — как в реальных образцах файлов

# Каталог полей реестра начислений (исходящий файл — кооператив -> банк).
# (ключ, подпись для UI, обязательно ли поле в формате)
CHARGE_FIELD_CATALOG = [
    ("account_number", "Лицевой счёт", True),
    ("payer_name", "ФИО плательщика", True),
    ("purpose", "Назначение платежа", True),
    ("service_code", "Код услуги/периода", False),
    ("amount", "Сумма", True),
    ("document_number", "Номер документа", False),
]

# Каталог полей реестра платежей (входящий файл — банк -> кооператив).
PAYMENT_FIELD_CATALOG = [
    ("date", "Дата", True),
    ("time", "Время", False),
    ("branch_code", "Код отделения", False),
    ("terminal_id", "ID терминала", False),
    ("operation_id", "Номер операции (для дедупликации)", False),
    ("account_number", "Лицевой счёт", True),
    ("payer_name", "ФИО плательщика", False),
    ("purpose", "Назначение платежа", False),
    ("service_code", "Код услуги/периода", False),
    ("charged_amount", "Сумма начисления", True),
    ("credited_amount", "Сумма зачисления (за вычетом комиссии)", False),
    ("fee_amount", "Комиссия банка", False),
    ("status_code", "Код статуса операции", False),
]

# Реальный формат из образцов файлов (см. докстринг выше) — используется,
# пока председатель не настроил свой (app/bank_sync.py: get_registry_format).
DEFAULT_CHARGE_COLUMNS = ["account_number", "payer_name", "purpose", "service_code", "amount"]
DEFAULT_PAYMENT_COLUMNS = [
    "date", "time", "branch_code", "terminal_id", "operation_id", "account_number",
    "payer_name", "purpose", "service_code", "charged_amount", "credited_amount",
    "fee_amount", "status_code",
]


@dataclasses.dataclass
class RegistryFormat:
    """Полная настройка формата файлов реестров для одного счёта —
    построена из BankRegistryFormat (models.py) в app/bank_sync.py:
    get_registry_format(), либо берётся как DEFAULT_FORMAT ниже, если
    председатель формат ещё не настраивал."""
    charge_columns: list[str]
    payment_columns: list[str]
    charge_decimal_separator: str = "."
    payment_decimal_separator: str = ","
    delimiter: str = ";"
    encoding: str = ENCODING
    # Строки реестра платежей, начинающиеся с этого префикса — не данные, а
    # итоговая сводка в конце файла (пример: "=4;5120,00;5038,08;81,92;
    # 945839;18-05-2026" — количество записей, суммы, номер реестра, дата) —
    # пропускаются при разборе, не превращаются в битую запись. Пустое
    # значение/None — в файлах этого банка сводки не бывает, не пропускать
    # ничего по префиксу.
    trailer_prefix: str | None = "="
    # Код услуги/периода, одинаковый у всех начислений одного счёта в
    # образцах (везде "0625") — деление начислений по разным кодам не
    # встретилось ни в одном из образцов, поэтому один общий код на счёт,
    # не за каждый вид взноса отдельно (упрощение, см. context.md).
    service_code: str = "0625"


DEFAULT_FORMAT = RegistryFormat(
    charge_columns=DEFAULT_CHARGE_COLUMNS, payment_columns=DEFAULT_PAYMENT_COLUMNS,
)


def build_charge_registry_file(items: list[ChargeRegistryItem], fmt: RegistryFormat = DEFAULT_FORMAT) -> bytes:
    lines = []
    for item in items:
        values = {
            "account_number": item.account_number,
            "payer_name": item.payer_name,
            "purpose": item.purpose,
            "service_code": item.service_code or fmt.service_code,
            "amount": _format_amount(item.amount, fmt.charge_decimal_separator),
            "document_number": item.document_number or "",
        }
        fields = [_escape(values.get(key, ""), fmt.delimiter) for key in fmt.charge_columns]
        lines.append(fmt.delimiter.join(fields))
    text = LINE_ENDING.join(lines) + LINE_ENDING if lines else ""
    return text.encode(fmt.encoding, errors="replace")


def parse_payment_registry_file(data: bytes, fmt: RegistryFormat = DEFAULT_FORMAT) -> list[PaymentRegistryItem]:
    """Пустые строки, итоговая сводка (см. `fmt.trailer_prefix`) и строки с
    недостаточным числом полей пропускаются, а не прерывают разбор всего
    файла — единичная битая строка не должна блокировать загрузку
    остальных, но и не должна тихо становиться нулевым платежом."""
    text = data.decode(fmt.encoding, errors="replace")
    items: list[PaymentRegistryItem] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if fmt.trailer_prefix and line.startswith(fmt.trailer_prefix):
            continue
        parts = [p.strip() for p in line.split(fmt.delimiter)]
        if len(parts) < len(fmt.payment_columns):
            continue
        row = dict(zip(fmt.payment_columns, parts))

        amount = _parse_amount(row.get("charged_amount"), fmt.payment_decimal_separator)
        if amount is None:
            continue
        account_number = row.get("account_number") or None
        operation_date = _parse_date(row.get("date")) or dt.date.today()
        operation_id = row.get("operation_id")
        external_id = operation_id or f"{account_number}:{row.get('date', '')}:{row.get('charged_amount', '')}"

        items.append(PaymentRegistryItem(
            external_id=external_id,
            account_number=account_number,
            payer_name=row.get("payer_name") or None,
            amount=amount,
            operation_date=operation_date,
            payment_purpose=row.get("purpose") or None,
            credited_amount=_parse_amount(row.get("credited_amount"), fmt.payment_decimal_separator),
            fee_amount=_parse_amount(row.get("fee_amount"), fmt.payment_decimal_separator),
        ))
    return items


def _escape(value: str, delimiter: str) -> str:
    # Если разделитель формата встретится внутри значения (например, в
    # названии/адресе), строка развалится на лишние колонки при разборе
    # банком — безопаснее заменить его пробелом, чем экранировать (формат
    # не текстовый CSV с кавычками, банк кавычки не разбирает).
    return (value or "").replace(delimiter, " ").replace("\r", " ").replace("\n", " ")


def _format_amount(value: Decimal, decimal_separator: str) -> str:
    text = f"{value:.2f}"
    return text.replace(".", decimal_separator) if decimal_separator != "." else text


def _parse_amount(raw: str | None, decimal_separator: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    normalized = raw.replace(decimal_separator, ".") if decimal_separator != "." else raw
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _parse_date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    for pattern in ("%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None
