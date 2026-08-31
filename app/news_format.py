"""
Форматирование текста новости: правление пишет в упрощённой markdown-разметке
(**жирный**, *курсив*, [ссылка](url), списки через "- ", ![alt](url) —
картинка), на выходе — санитизированный HTML (bleach) и обрезанное текстовое
превью для главной.

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
from markupsafe import Markup

ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i", "u", "a", "ul", "ol", "li",
    "blockquote", "code", "pre", "h3", "h4", "hr", "img",
]
ALLOWED_ATTRS = {"a": ["href", "title"], "img": ["src", "alt"]}

_md = md_lib.Markdown(extensions=["nl2br"])

EXCERPT_LENGTH = 400


def render_html(text: str) -> Markup:
    """Markdown -> безопасный HTML для отображения новости целиком."""
    _md.reset()
    html = _md.convert(text or "")
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    clean = bleach.linkify(clean, callbacks=[*bleach.linkifier.DEFAULT_CALLBACKS])
    return Markup(clean)


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
