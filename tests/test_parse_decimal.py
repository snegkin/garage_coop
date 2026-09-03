"""
parse_decimal/parse_optional_decimal (app/i18n.py) — разбор дробного числа,
введённого пользователем, с поддержкой и точки, и запятой как разделителя
дробной части (fmt2 выводит с запятой для ru-локали, значит и ввести
обратно нужно уметь то же самое число с запятой).
"""
from decimal import Decimal, InvalidOperation

import pytest

from app.i18n import parse_decimal, parse_optional_decimal


@pytest.mark.parametrize("raw, expected", [
    ("1234,56", Decimal("1234.56")),
    ("1234.56", Decimal("1234.56")),
    ("0,5", Decimal("0.5")),
    ("100", Decimal("100")),
    ("-12,50", Decimal("-12.50")),
    (" 12,5 ", Decimal("12.5")),
    ("1,234.56", Decimal("1234.56")),  # британский формат: запятая — тысячи, точка — дробная часть
    ("1.234,56", Decimal("1234.56")),  # русский/немецкий формат группировки тысяч точкой
    ("1 234,56", Decimal("1234.56")),  # пробел-группировка тысяч + запятая-дробь
    ("1\xa0234,56", Decimal("1234.56")),  # неразрывный пробел (копипаста из таблицы)
    ("1,234,567", Decimal("1234567")),  # несколько запятых — точно группировка тысяч
    ("1.234.567", Decimal("1234567")),  # несколько точек — тоже группировка тысяч
])
def test_parse_decimal_accepts_both_separators(raw, expected):
    assert parse_decimal(raw) == expected


def test_parse_decimal_rejects_garbage():
    with pytest.raises(InvalidOperation):
        parse_decimal("не число")


def test_parse_optional_decimal_empty_is_none():
    assert parse_optional_decimal("") is None
    assert parse_optional_decimal(None) is None


def test_parse_optional_decimal_invalid_is_none():
    assert parse_optional_decimal("abc") is None


def test_parse_optional_decimal_comma_works():
    assert parse_optional_decimal("1234,56") == Decimal("1234.56")
