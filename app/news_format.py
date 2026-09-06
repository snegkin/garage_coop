"""
Форматирование текста новости: правление пишет в упрощённой markdown-разметке
(**жирный**, *курсив*, [ссылка](url), списки через "- ", ![alt](url) —
картинка, ||текст|| — скрытый текст, раскрывается по клику), на выходе —
санитизированный HTML (bleach) и обрезанное текстовое превью для главной.

Переиспользуется и для вики (app/wiki.py: render_wiki_html) — модуль не
завязан на модель News.

Вставка картинки (![](url)) — обычный markdown, обрабатывается стандартным
`markdown` без расширений (это часть core-синтаксиса). URL, который туда
попадает через тулбар "Вставить картинку" в форме — адрес уже загруженного
вложения (см. news.py: /news/attachments/upload, wiki.py:
/wiki/attachments/upload). Ничто не мешает автору вписать и внешний URL
руками — как и с обычной ссылкой [текст](url), это осознанно разрешено
(автор — доверенный член правления), bleach всё равно проверяет схему
(http/https/mailto), javascript:-протокол невозможен.
"""
import re

import bleach
import markdown as md_lib
from markupsafe import Markup, escape

from .i18n import translate as _

ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i", "u", "a", "ul", "ol", "li",
    "blockquote", "code", "pre", "h3", "h4", "hr", "img", "span",
]
ALLOWED_ATTRS = {"a": ["href", "title"], "img": ["src", "alt"], "span": ["class", "tabindex", "role"]}

_md = md_lib.Markdown(extensions=["nl2br"])

EXCERPT_LENGTH = 400

# Скрытый текст — ||текст|| (например, пароль устройства): по умолчанию
# залит цветом-заглушкой (см. base.html: .wiki-spoiler), раскрывается по
# клику через делегированный JS-обработчик там же. НЕ разметка доступа —
# текст всё равно есть в HTML страницы, только визуально скрыт (сам автор
# так и просил: не мелькать на экране, не "защитить"). Разбирается ДО
# _md.convert() на сыром тексте, содержимое HTML-экранируется явно (даже
# если внутри оказались символы вроде "<" — safe-mode тут ни при чём,
# просто получившийся <span> не должен ломать разметку страницы).
_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|")


def _spoiler_sub(match: re.Match) -> str:
    return f'<span class="wiki-spoiler" tabindex="0" role="button">{escape(match.group(1))}</span>'


# bleach.linkify() сам находит URL внутри уже собранного <span
# class="wiki-spoiler">...</span> и оборачивает его в <a> — у ссылки свой
# цвет (a { color: ... } в base.html побеждает унаследованный от
# .wiki-spoiler, т.к. унаследованное значение проигрывает любому явно
# совпавшему правилу), и маскировка «текст того же цвета, что фон»
# ломается — спрятанный URL виден как обычная ссылка. У linkify() есть
# параметр skip_tags для ровно такого случая, но на практике он ломает
# другое: содержимое пропускаемого тега на выходе экранируется ВТОРОЙ раз
# (bleach 6.x, "&lt;" -> "&amp;lt;"). Поэтому прячем уже собранные спойлеры
# текстовыми плейсхолдерами ДО linkify и возвращаем обратно ПОСЛЕ — сам
# linkify их содержимое вообще не видит.
_SPOILER_SPAN_RE = re.compile(r'<span class="wiki-spoiler"[^>]*>.*?</span>')
_SPOILER_PLACEHOLDER_RE = re.compile(r"SPOILERSTASH(\d+)ENDSTASH")

# bleach.linkifier.TLDS (встроенный список доменных зон, по которым linkify
# распознаёт ссылку БЕЗ протокола — просто "домен.зона") по ошибке содержит
# "xn" отдельной зоной — это ACE/punycode-префикс "xn--" (см. RFC 3492,
# используется для не-ASCII доменов вроде кириллического ".рф"), а не сама
# зона. Из-за этого punycode-домен целиком (например,
# "xn----dtbbg1boax0b.xn--p1ai") обрывался ровно на этом "xn":
# "...xn----dtbbg1boax0b.xn" уходило в ссылку, а "--p1ai" оставалось
# обычным текстом (воспроизведено и проверено). Чинится своим списком зон:
# убираем бесполезную "xn", добавляем regex-альтернативу для ЛЮБОЙ
# punycode-зоны — bleach просто склеивает список через "|" без экранирования
# каждого элемента, так что валидный фрагмент регулярки в списке работает
# как есть, отдельный список punycode-зон целиком (там их сотни) не нужен.
_TLDS_WITH_PUNYCODE = [tld for tld in bleach.linkifier.TLDS if tld != "xn"] + ["xn--[a-z0-9]+"]
_LINKER = bleach.linkifier.Linker(
    url_re=bleach.linkifier.build_url_re(tlds=_TLDS_WITH_PUNYCODE),
    callbacks=[*bleach.linkifier.DEFAULT_CALLBACKS],
)

# Длинный блок кода (```/отступ в markdown -> <pre><code>...</code></pre>) —
# сворачивается под кнопку "Показать полностью", чтобы при обзорном чтении
# статьи не приходилось скроллить длинный листинг целиком (см. .wiki-code-block
# в base.html — там же делегированный JS-обработчик клика по кнопке).
# Короткие блоки (в пределах порога) не трогаются — оборачивать их незачем.
CODE_BLOCK_COLLAPSE_LINES = 12
_CODE_BLOCK_RE = re.compile(r"<pre><code>.*?</code></pre>", re.DOTALL)


def _wrap_long_code_block(match: re.Match) -> str:
    pre_html = match.group(0)
    line_count = pre_html.count("\n")
    if line_count <= CODE_BLOCK_COLLAPSE_LINES:
        return pre_html
    collapsed_label = _("Показать полностью ({n} строк)", n=line_count)
    expanded_label = _("Свернуть")
    return (
        '<div class="wiki-code-block is-collapsed">'
        f"{pre_html}"
        '<button type="button" class="btn btn-sm btn-outline-secondary wiki-code-toggle" '
        f'data-collapsed-label="{escape(collapsed_label)}" data-expanded-label="{escape(expanded_label)}">'
        f"{escape(collapsed_label)}</button></div>"
    )


def render_html(text: str, collapse_long_code: bool = False) -> Markup:
    """Markdown -> безопасный HTML для отображения новости/страницы вики
    целиком. collapse_long_code — сворачивать ли длинные блоки кода под
    кнопку (см. _wrap_long_code_block); включено только для готовой
    страницы вики (app/__init__.py: render_wiki_html) и её предпросмотра
    (wiki.py: preview) — НЕ для новостей и НЕ для исходящей почты
    (mailbox.py: compose), где кнопка сворачивания не сможет работать
    (нет JS в письме) и просто обрежет часть кода без возможности
    развернуть."""
    _md.reset()
    pre = _SPOILER_RE.sub(_spoiler_sub, text or "")
    html = _md.convert(pre)
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)

    stash: list[str] = []

    def _stash(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"SPOILERSTASH{len(stash) - 1}ENDSTASH"

    stashed = _SPOILER_SPAN_RE.sub(_stash, clean)
    linked = _LINKER.linkify(stashed)
    final = _SPOILER_PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], linked)
    if collapse_long_code:
        final = _CODE_BLOCK_RE.sub(_wrap_long_code_block, final)
    return Markup(final)


def plain_text(text: str) -> str:
    """Markdown -> обычный текст без разметки (для превью)."""
    _md.reset()
    html = _md.convert(text or "")
    stripped = bleach.clean(html, tags=[], strip=True)
    return re.sub(r"\s+", " ", stripped).strip()


def excerpt(text: str, max_chars: int = EXCERPT_LENGTH) -> tuple[str, bool]:
    """Обрезанное превью без разметки и признак того, что текст обрезан
    (используется на главной странице для ссылки "Читать дальше")."""
    full = plain_text(text)
    if len(full) <= max_chars:
        return full, False
    cut = full[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:")
    return cut + "…", True
