"""
Рендер контактных данных кликабельными ссылками (app/contact_format.py).

Отдельный фокус — phone_link с несколькими номерами через запятую:
Counterparty.phone (в отличие от Person, у которого отдельная таблица
Phone на каждый номер) — одно текстовое поле, несколько телефонов вводят
в него через запятую. Без разбора все номера слипались бы в один
нерабочий tel: (баг, из-за которого несколько телефонов в карточке
контрагента выглядели одним "супертелефоном").
"""
from app.contact_format import phone_link


def test_phone_link_single_number():
    result = str(phone_link("+7 911 111-11-11"))
    assert result == '<a href="tel:+79111111111">+7 911 111-11-11</a>'


def test_phone_link_none_returns_dash():
    assert phone_link(None) == "—"


def test_phone_link_splits_multiple_numbers_on_comma():
    result = str(phone_link("+7 911 111-11-11, +7 922 222-22-22"))
    assert result == (
        '<a href="tel:+79111111111">+7 911 111-11-11</a>, '
        '<a href="tel:+79222222222">+7 922 222-22-22</a>'
    )
    # каждый номер — своя отдельная ссылка, а не один слипшийся tel:
    assert "tel:+79111111111+79222222222" not in result


def test_phone_link_splits_on_semicolon_too():
    result = str(phone_link("+7 911 111-11-11; +7 922 222-22-22"))
    assert result.count('<a href="tel:') == 2


def test_phone_link_ignores_empty_parts_from_trailing_separator():
    result = str(phone_link("+7 911 111-11-11, "))
    assert result.count('<a href="tel:') == 1
