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


def render_html(text: str) -> Markup:
    """Markdown -> безопасный HTML для отображения новости целиком."""
    _md.reset()
    pre = _SPOILER_RE.sub(_spoiler_sub, text or "")
    html = _md.convert(pre)
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
