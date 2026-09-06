"""
Кликабельные логины/телефоны/ФИО в тексте журнала аудита (governance.audit_log).

Записи журнала — обычные русские предложения с именами/логинами/номерами,
собранные множеством разных audit.record(...) по всему коду (см. docstring
audit.record) — не структурированные данные со ссылкой на конкретную
сущность, в отличие, например, от comment_format.linkify_related_person
(та знает related_person_id заранее). Поэтому ссылки здесь строятся поиском
по АКТУАЛЬНЫМ данным на момент ПРОСМОТРА журнала: сам текст записи не
меняется (это факт истории на момент события), но если с тех пор логин
или ФИО сменились — ссылка ведёт на текущую карточку под текущим именем.

Правила (во всех вызовах audit.record в коде логины и телефоны пишутся в
кавычках «...», ФИО/краткое имя — как есть, без кавычек):
- логин — только внутри «кавычек» и только при точном совпадении с
  User.username: голое слово без кавычек не линкуем, слишком велик риск
  случайно превратить в ссылку обычный текст записи;
- телефон — тоже только внутри «кавычек», сравнение по нормализованным
  цифрам (auth._normalize_phone_digits), а не буквально — в summary
  телефон записан ровно так, как его ввёл человек при попытке входа;
- ФИО и краткое имя (Фамилия И.О.) — прямой поиск подстроки по ВСЕМ людям
  сразу (тот же приём, что и linkify_related_person), самые длинные имена
  проверяются первыми, чтобы короткое имя не «откусило» кусок более
  длинного при пересечении (напр. «Иванов И.» внутри «Иванов Иван И.»).
"""
import re

from flask import url_for
from markupsafe import Markup, escape

from .auth import _normalize_phone_digits

_QUOTED_RE = re.compile(r"«([^»]+)»")


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


def build_linkify_index(persons, users) -> dict:
    """
    Готовит справочники один раз на весь журнал (не на каждую запись) —
    persons/users передаются извне, чтобы не гонять запросы к БД повторно.
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

    names_re = None
    if name_to_url:
        names_by_length = sorted(name_to_url, key=len, reverse=True)
        names_re = re.compile("|".join(re.escape(name) for name in names_by_length))

    return {
        "username_to_url": username_to_url,
        "phone_to_url": phone_to_url,
        "name_to_url": name_to_url,
        "names_re": names_re,
    }


def linkify_summary(summary: str | None, index: dict) -> Markup | str | None:
    if not summary:
        return summary

    username_to_url = index["username_to_url"]
    phone_to_url = index["phone_to_url"]

    def _quoted_repl(m: "re.Match[str]") -> str:
        inner = m.group(1)
        url = username_to_url.get(inner)
        if url is None:
            digits = _normalize_phone_digits(inner)
            if digits:
                url = phone_to_url.get(digits)
        if url is None:
            return m.group(0)
        return f'«<a href="{url}">{inner}</a>»'

    # escape() — самая первая обработка сырого текста (защита от обычных
    # <>&"' в свободных полях вроде комментария к причине архивации),
    # дальше обе подстановки работают уже по безопасной строке и вставляют
    # только собственный, доверенный HTML (<a href="...">).
    text = str(escape(summary))
    text = _QUOTED_RE.sub(_quoted_repl, text)

    names_re = index["names_re"]
    if names_re is not None:
        name_to_url = index["name_to_url"]

        def _name_repl(m: "re.Match[str]") -> str:
            name = m.group(0)
            return f'<a href="{name_to_url[name]}">{name}</a>'

        text = names_re.sub(_name_repl, text)

    return Markup(text)
