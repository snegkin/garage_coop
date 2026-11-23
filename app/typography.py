"""Неразрывная типографика документов на бланке.

Чтобы в шапке и тексте документа не разъезжались по строкам сущности,
которые на двух строках смотрятся плохо: «КоАП / РФ», «ст. / 17.10»,
«серия 12 34 / № 567890», «23 / ноября 2026 г.», «Иванов / И. И.»,
адрес сайта или почты, разорванный на «@» или «-», телефон
«+7 (912) 345-/67-89».

Пробелы внутри таких сущностей меняются на неразрывные (U+00A0 — то же,
что Ctrl+Shift+Space в Word; в редакторе листа — Shift+Space), а то,
что рвётся по дефису/«@»/«/», — адреса, почта, телефоны — заворачивается
в <span style="white-space: nowrap">. Обрабатывается только текст между
тегами; текст, уже лежащий в таком span, не трогается — фильтр можно
применять к одному HTML сколько угодно раз.
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

NBSP = " "
# Так же, как его сохраняет санитайзер (applications.sanitize_html), —
# после правки в редакторе и сохранения разметка не меняется.
NOWRAP_OPEN = '<span style="white-space: nowrap;">'

_TOKEN_RE = re.compile(r"(<[^>]*>)")
_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)")
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "col", "wbr"}
_NOWRAP_STYLE_RE = re.compile(r"white-space\s*:\s*(?:nowrap|pre)", re.IGNORECASE)

# Пробел в тексте: обычный, неразрывный (символом или сущностью).
_S = r"(?:[ \t ]|&nbsp;|&#160;)+"

_TLD = r"(?:ru|рф|su|рус|com|org|net|info|online|site|pro|biz|io|me)"
_NOWRAP_RE = re.compile(
    "|".join([
        # Электронная почта.
        r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+",
        # Адрес сайта: со схемой, с www или просто домен из известной зоны.
        r"(?:https?://|www\.)[^\s<>\" ]+",
        r"(?<![\w@.\-/])[\w\-]+(?:\.[\w\-]+)*\." + _TLD + r"(?![\w\-])(?:/[^\s<>\" ]*)?",
        # Телефон: +7 (912) 345-67-89, 8 912 345 67 89, 8-800-…
        r"(?<![\w\-])(?:\+7|8)[  \-]?\(?\d{3,5}\)?[  \-]?\d{1,3}(?:[  \-]?\d{2}){2}(?!\w)",
    ]),
    re.IGNORECASE,
)
# Точка/запятая в конце адреса — знак препинания предложения, не адреса.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)»]+$")

_MONTHS = ("января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря|"
           "январь|февраль|март|апрель|май|июнь|июль|август|сентябрь|октябрь|ноябрь|декабрь")
# Сокращения, которые не отрываются от следующего за ними номера/слова.
_ABBR = (r"№|§|ст\.|ч\.|п\.|пп\.|подп\.|абз\.|гл\.|разд\.|прил\.|д\.|кв\.|ул\.|пр\.|пр-т|пер\.|"
         r"наб\.|пл\.|ш\.|мкр\.|р-н|обл\.|корп\.|стр\.|оф\.|пом\.|эт\.|тел\.|т\.|с\.|им\.|рис\.|табл\.|"
         r"серия|сер\.|номер|бокс|гараж|гаража|гаражу|гаражом|счёт|сч\.|л/с|ИНН|КПП|ОГРН|ОГРНИП|БИК|ОКПО|"
         r"ОКТМО|СНИЛС|р/с|к/с")
# Единицы и слова, которые не отрываются от числа перед ними.
_UNITS = (r"г\.|гг\.|год[а-я]*|лет|руб\.?|рубл[а-яё]*|р\.|коп\.?|копе[а-яё]*|₽|%|кВт(?:·ч|\*ч|ч)?|Вт|"
          r"кВА|А|В|м²|м2|м|кв\.|см|мм|км|кг|т\.|шт\.?|чел\.?|дн\.|дн[а-яё]*|мес\.?|месяц[а-яё]*|"
          r"ч\.|мин\.?|сек\.?|тыс\.?|млн|млрд|" + _MONTHS)
# Кодексы и законы: «КоАП РФ», «ГК РФ», «ФЗ № …».
_CODES = r"КоАП|ГК|НК|УК|ЖК|ЗК|ТК|ГПК|АПК|УПК|КАС|СК|ГрК|ФЗ"

_RULES: list[tuple[re.Pattern, str]] = [
    # Неразрывные пробелы внутри числа и между группами цифр:
    # «12 345,67», «серия 12 34», «45 06 123456».
    (re.compile(r"(?<=\d)" + _S + r"(?=\d)"), NBSP),
    # «КоАП РФ», «ГК РФ», «Федеральный закон РФ».
    (re.compile(r"(?<=\w)" + _S + r"(?=(?:РФ|России)\b)"), NBSP),
    # «КоАП РФ 17.10», «ГК РФ ст. 123».
    (re.compile(r"(?<![\w])(РФ)" + _S + r"(?=\d|ст\.)"), r"\1" + NBSP),
    # «ст. 17.10», «№ 5», «п. 3», «серия 1234», «ИНН 6600…», «ул. Ленина».
    (re.compile(r"(?<![\w\-/])(" + _ABBR + r")" + _S + r"(?=\S)", re.IGNORECASE), r"\1" + NBSP),
    # «г. Екатеринбург», но «2026 г. Иванов» — «г.» после числа означает
    # «год», не «город»: к следующему слову не клеится (к числу — правилом
    # единиц ниже).
    (re.compile(r"(?<![\w\-/])(?<!\d )(?<!\d\u00a0)(гг?\.)" + _S + r"(?=\S)"), r"\1" + NBSP),
    # «КоАП РФ 17.10», «ГК РФ ст.» — сокращение кодекса не отрывается
    # от следующего слова/номера.
    (re.compile(r"(?<![\w\-])(" + _CODES + r")" + _S + r"(?=\S)"), r"\1" + NBSP),
    # Число и то, что к нему относится: «2026 г.», «100 руб.», «23 ноября»,
    # «15 кВт·ч», «10 дней».
    (re.compile(r"(?<=\d)" + _S + r"(?=(?:" + _UNITS + r")(?![\w]))", re.IGNORECASE), NBSP),
    # «ноября 2026», «января 2025 г.»
    (re.compile(r"(?<![\w])(" + _MONTHS + r")" + _S + r"(?=\d)", re.IGNORECASE), r"\1" + NBSP),
    # Инициалы: «И. И.», «Иванов И. И.», «И. И. Иванов».
    (re.compile(r"(?<![\w])([А-ЯЁA-Z]\.)" + _S + r"(?=[А-ЯЁA-Z]\.)"), r"\1" + NBSP),
    (re.compile(r"(?<=[а-яёa-z])" + _S + r"(?=[А-ЯЁA-Z]\.(?:" + _S + r")?[А-ЯЁA-Z]\.)"), NBSP),
    (re.compile(r"(?<![\w])([А-ЯЁA-Z]\.)" + _S + r"(?=[А-ЯЁ][а-яё])"), r"\1" + NBSP),
    # Тире не начинает строку: «оплата — 100 руб.»
    (re.compile(r"(?<=\S)" + _S + r"(?=[—–]\s)"), NBSP),
    # Короткий предлог/союз не висит в конце строки: «в гараже», «к ГСК».
    (re.compile(r"(?<![\w\-.])([вксуояиаВКСУОЯИА]|во|ко|со|на|по|за|от|до|из|об|о|не|ни|же)" + _S + r"(?=[\w«(№\"])"),
     r"\1" + NBSP),
]


def _nowrap(match: re.Match) -> str:
    text = match.group(0)
    tail = _TRAILING_PUNCT_RE.search(text)
    rest = tail.group(0) if tail else ""
    text = text[:len(text) - len(rest)]
    if not text:
        return match.group(0)
    return NOWRAP_OPEN + text + "</span>" + rest


def _text(text: str) -> str:
    """Один кусок текста (уже HTML-экранированный): без тегов внутри."""
    if not text.strip():
        return text
    # Адреса/почта/телефоны — целиком в nowrap, остальное — неразрывными
    # пробелами; правила к содержимому nowrap не применяются (там свои
    # дефисы и точки — «gsk-vostok.ru» не должен становиться «gsk-vostok. ru»).
    parts = []
    pos = 0
    for match in _NOWRAP_RE.finditer(text):
        parts.append(_spaces(text[pos:match.start()]))
        parts.append(_nowrap(match))
        pos = match.end()
    parts.append(_spaces(text[pos:]))
    return "".join(parts)


def _spaces(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def nobreak_html(html: str | None) -> Markup:
    """HTML документа (уже очищенный) → то же с неразрывными пробелами
    и nowrap-обёртками. Теги и атрибуты не меняются."""
    out = []
    stack: list[bool] = []  # открытые теги: внутри nowrap?
    for token in _TOKEN_RE.split(str(html or "")):
        if not token:
            continue
        if token.startswith("<"):
            out.append(token)
            name = _TAG_NAME_RE.match(token)
            if not name or token.startswith("<!") or token.endswith("/>"):
                continue
            closing, tag = name.group(1), name.group(2).lower()
            if tag in _VOID_TAGS:
                continue
            if closing:
                if stack:
                    stack.pop()
            else:
                stack.append(bool(_NOWRAP_STYLE_RE.search(token)))
            continue
        out.append(token if any(stack) else _text(token))
    return Markup("".join(out))


def nobreak(value) -> Markup:
    """Jinja-фильтр: Markup — как HTML, обычная строка — экранируется."""
    if value is None:
        return Markup("")
    if not isinstance(value, Markup) and not hasattr(value, "__html__"):
        value = escape(str(value))
    return nobreak_html(str(value))
