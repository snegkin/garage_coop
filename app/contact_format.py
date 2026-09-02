"""
Рендер контактных данных (телефон/email/telegram) кликабельными ссылками
(tel:/mailto:/t.me) для шаблонов. Поля в БД (Person.email, Person.telegram,
Phone.number, Counterparty.phone/email) — свободный текст без валидации
формата (см. app/persons.py: значения берутся как есть из формы), поэтому
все функции ниже терпимы к произвольному мусору во входной строке.
"""
import re

from markupsafe import Markup

_PHONE_ALLOWED = re.compile(r"[^0-9+]")


def phone_link(number: str | None) -> Markup | str:
    """<a href="tel:...">исходный текст номера</a>; для tel: оставляем только цифры и '+'."""
    if not number:
        return "—"
    digits = _PHONE_ALLOWED.sub("", number)
    if not digits:
        return Markup("{}").format(number)
    return Markup('<a href="tel:{}">{}</a>').format(digits, number)


def phones_html(phones) -> Markup | str:
    """phones — список объектов Phone (person.phones)."""
    numbers = [p.number for p in phones if p.number]
    if not numbers:
        return "—"
    return Markup(", ").join(phone_link(n) for n in numbers)


def email_link(address: str | None) -> Markup | str:
    if not address:
        return "—"
    address = address.strip()
    return Markup('<a href="mailto:{}">{}</a>').format(address, address)


def telegram_link(handle: str | None) -> Markup | str:
    """
    handle хранится как ввёл пользователь: "@username", "username" или уже
    полная ссылка (https://t.me/username). Ссылку строим сами по фиксированной
    схеме t.me, поэтому произвольная схема (javascript: и т.п.) в поле не
    опасна — используется только как отображаемый текст.
    """
    if not handle:
        return "—"
    handle = handle.strip()
    if handle.startswith("http://") or handle.startswith("https://"):
        url = handle
    else:
        url = f"https://t.me/{handle.lstrip('@')}"
    return Markup('<a href="{}" target="_blank" rel="noopener">{}</a>').format(url, handle)
