"""
Кликабельные логины/телефоны/ФИО/номера лицевых счетов в тексте журнала
аудита (governance.audit_log).

Записи журнала — обычные русские предложения с именами/логинами/номерами,
собранные множеством разных audit.record(...) по всему коду (см. docstring
audit.record) — не структурированные данные со ссылкой на конкретную
сущность, в отличие, например, от comment_format.linkify_related_person
(та знает related_person_id заранее). Поэтому ссылки здесь строятся поиском
по АКТУАЛЬНЫМ данным на момент ПРОСМОТРА журнала: сам текст записи не
меняется (это факт истории на момент события), но если с тех пор логин,
ФИО или владелец счёта сменились — ссылка ведёт на текущую карточку.

Правила (во всех вызовах audit.record в коде логины и телефоны пишутся в
кавычках «...», ФИО/краткое имя и номера счетов — как есть, без кавычек):
- логин — только внутри «кавычек» и только при точном совпадении с
  User.username: голое слово без кавычек не линкуем, слишком велик риск
  случайно превратить в ссылку обычный текст записи;
- телефон — тоже только внутри «кавычек», сравнение по нормализованным
  цифрам (auth._normalize_phone_digits), а не буквально — в summary
  телефон записан ровно так, как его ввёл человек при попытке входа;
- ФИО и краткое имя (Фамилия И.О.) — прямой поиск подстроки по ВСЕМ людям
  сразу (тот же приём, что и linkify_related_person), самые длинные имена
  проверяются первыми, чтобы короткое имя не «откусило» кусок более
  длинного при пересечении (напр. «Иванов И.» внутри «Иванов Иван И.»);
- номер лицевого счёта — отдельно стоящая последовательность цифр (\\b\\d+\\b),
  сравнение точное с реальным MemberAccount/PersonalAccount.account_number.

Все четыре вида ищутся ОДНИМ комбинированным регэкспом за один проход по
исходному тексту (не последовательными подстановками одна поверх другой):
иначе номер счёта, случайно совпавший с id в уже вставленном
`<a href="/persons/12">`, задвоил бы ссылку и сломал разметку.
"""
import re

from flask import url_for
from markupsafe import Markup, escape

from .auth import _normalize_phone_digits


def _unambiguous(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """
    Из списка (текст, url) собирает словарь, ИСКЛЮЧАЯ тексты, которые
    встретились у РАЗНЫХ url (общий телефон на семью, однофамильцы с
    одинаковым кратким именем «Иванов И.И.» и т.п.) — тот же принцип, что
    и у auth._person_by_phone_digits: при неоднозначности безопаснее не
    дать ссылку вовсе, чем угадать не того человека.
    """
    urls_by_text: dict[str, set[str]] = {}
    for text, url in pairs:
        urls_by_text.setdefault(text, set()).add(url)
    return {text: next(iter(urls)) for text, urls in urls_by_text.items() if len(urls) == 1}


def build_linkify_index(persons, users, member_accounts=(), personal_accounts=()) -> dict:
    """
    Готовит справочники один раз на весь журнал (не на каждую запись) —
    все объекты передаются извне, чтобы не гонять запросы к БД повторно.
    """
    username_to_url = {
        u.username: url_for("persons.detail", person_id=u.person_id)
        for u in users if u.person_id and u.username
    }  # User.username уникален в БД — неоднозначности здесь в принципе быть не может

    phone_pairs: list[tuple[str, str]] = []
    name_pairs: list[tuple[str, str]] = []
    for person in persons:
        url = url_for("persons.detail", person_id=person.id)
        for phone in person.phones:
            digits = _normalize_phone_digits(phone.number)
            if digits:
                phone_pairs.append((digits, url))
        if person.full_name:
            name_pairs.append((person.full_name, url))
        short = person.short_name
        if short and short != person.full_name:
            name_pairs.append((short, url))

    phone_to_url = _unambiguous(phone_pairs)
    name_to_url = _unambiguous(name_pairs)

    # Номер лицевого счёта после смены собственника переходит новому счёту,
    # а прежний (архивный) остаётся с тем же номером для истории — это НЕ
    # случайная неоднозначность вроде общего телефона, а осознанное
    # устройство (см. docstring MemberAccount.is_archived), поэтому вместо
    # отказа от ссылки предпочитаем активный счёт: архивные добавляются в
    # словарь первыми, активные — следом и перезаписывают их.
    account_number_to_url: dict[str, str] = {}
    for ma in sorted(member_accounts, key=lambda a: a.is_archived, reverse=True):
        if ma.account_number:
            account_number_to_url[ma.account_number] = url_for("finance.member_account_detail", account_id=ma.id)
    for pa in personal_accounts:
        if pa.account_number:
            account_number_to_url[pa.account_number] = url_for("garages.detail", garage_id=pa.garage_id)

    name_alternation = "|".join(re.escape(name) for name in sorted(name_to_url, key=len, reverse=True))
    pattern = r"«[^»]+»|\b\d+\b"
    if name_alternation:
        pattern += "|" + name_alternation
    token_re = re.compile(pattern)

    return {
        "username_to_url": username_to_url,
        "phone_to_url": phone_to_url,
        "name_to_url": name_to_url,
        "account_number_to_url": account_number_to_url,
        "token_re": token_re,
    }


def linkify_summary(summary: str | None, index: dict) -> Markup | str | None:
    if not summary:
        return summary

    username_to_url = index["username_to_url"]
    phone_to_url = index["phone_to_url"]
    name_to_url = index["name_to_url"]
    account_number_to_url = index["account_number_to_url"]

    def _repl(m: "re.Match[str]") -> str:
        token = m.group(0)

        if token[0] == "«" and token[-1] == "»":
            inner = token[1:-1]
            url = username_to_url.get(inner)
            if url is None:
                digits = _normalize_phone_digits(inner)
                if digits:
                    url = phone_to_url.get(digits)
            if url is None:
                url = name_to_url.get(inner)  # напр. «Иванов И.И.» — краткое имя тоже пишут в кавычках
            if url is None:
                return token
            return f'«<a href="{url}">{inner}</a>»'

        url = name_to_url.get(token) or account_number_to_url.get(token)
        if url is None:
            return token
        return f'<a href="{url}">{token}</a>'

    # escape() — самая первая обработка сырого текста (защита от обычных
    # <>&"' в свободных полях вроде комментария к причине архивации);
    # весь дальнейший разбор идёт уже по безопасной строке ОДНИМ проходом
    # (см. docstring модуля — почему не несколько последовательных).
    text = str(escape(summary))
    text = index["token_re"].sub(_repl, text)
    return Markup(text)
