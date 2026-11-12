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
from html.parser import HTMLParser

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

# Код (`инлайн` или отступ/```-блок, markdown даёт в обоих случаях
# <code>...</code>, во втором случае — обёрнутый ещё и в <pre>) — тем же
# приёмом стэша, что и спойлеры выше, исключается из linkify целиком.
# Нужно на практике: bleach.linkifier даже с починенным списком TLD (см.
# _TLDS_WITH_PUNYCODE ниже) не универсален для ЛЮБОГО punycode-домена —
# например "pravlenie@xn----dtbbg1boax0b.xn--p1ai" всё равно линкуется не
# целиком, а с середины домена ("...@xn" остаётся текстом, дальше —
# ссылка) — воспроизведено и не чинится дальнейшей правкой regex-а TLD,
# это уже про то, где именно внутренний regex bleach решает, что начинается
# домен. Вместо борьбы с этим конкретным случаем — общий выход для автора
# статьи: обернуть техническую строку (логин, punycode-домен, что угодно)
# в код, чтобы поиск ссылок в ней не производился вообще.
_CODE_TAG_RE = re.compile(r"<code>.*?</code>", re.DOTALL)
_CODE_PLACEHOLDER_RE = re.compile(r"CODESTASH(\d+)ENDSTASH")

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

# Каждый блок кода (```/отступ в markdown -> <pre><code>...</code></pre>)
# оборачивается в .wiki-code-block — даёт кнопку "Скопировать" (наведение,
# верхний правый угол — см. .wiki-copy-btn в base.html и делегированный
# JS-обработчик там же) ЛЮБОМУ блоку, а длинный — ДОПОЛНИТЕЛЬНО сворачивается
# под кнопку "Показать полностью", чтобы при обзорном чтении статьи не
# приходилось скроллить длинный листинг целиком.
CODE_BLOCK_COLLAPSE_LINES = 12
_CODE_BLOCK_RE = re.compile(r"<pre><code>.*?</code></pre>", re.DOTALL)


def _wrap_long_code_block(match: re.Match) -> str:
    pre_html = match.group(0)
    line_count = pre_html.count("\n")
    copy_label = _("Скопировать")
    copied_label = _("Скопировано")
    copy_button = (
        '<button type="button" class="wiki-copy-btn" '
        f'data-copy-label="{escape(copy_label)}" data-copied-label="{escape(copied_label)}">'
        f"{escape(copy_label)}</button>"
    )
    if line_count <= CODE_BLOCK_COLLAPSE_LINES:
        return f'<div class="wiki-code-block">{copy_button}{pre_html}</div>'
    collapsed_label = _("Показать полностью ({n} строк)", n=line_count)
    expanded_label = _("Свернуть")
    return (
        '<div class="wiki-code-block is-collapsed">'
        f"{copy_button}{pre_html}"
        '<button type="button" class="btn btn-sm btn-outline-secondary wiki-code-toggle" '
        f'data-collapsed-label="{escape(collapsed_label)}" data-expanded-label="{escape(expanded_label)}">'
        f"{escape(collapsed_label)}</button></div>"
    )


def render_html(text: str, collapse_long_code: bool = False) -> Markup:
    """Markdown -> безопасный HTML для отображения новости/страницы вики
    целиком. collapse_long_code — оборачивать ли блоки кода в
    .wiki-code-block (см. _wrap_long_code_block): кнопка "Скопировать" —
    у любого блока, а длинные ДОПОЛНИТЕЛЬНО сворачиваются под кнопку
    "Показать полностью"; включено только для готовой страницы вики
    (app/__init__.py: render_wiki_html) и её предпросмотра (wiki.py:
    preview) — НЕ для новостей и НЕ для исходящей почты (mailbox.py:
    compose), где обе кнопки не смогут работать (нет JS в письме) и
    свернутый текст/кнопку без возможности развернуть/скопировать в
    один клик лучше не показывать вовсе."""
    _md.reset()
    pre = _SPOILER_RE.sub(_spoiler_sub, text or "")
    html = _md.convert(pre)
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)

    stash: list[str] = []

    def _stash(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"SPOILERSTASH{len(stash) - 1}ENDSTASH"

    stashed = _SPOILER_SPAN_RE.sub(_stash, clean)

    code_stash: list[str] = []

    def _stash_code(match: re.Match) -> str:
        code_stash.append(match.group(0))
        return f"CODESTASH{len(code_stash) - 1}ENDSTASH"

    stashed = _CODE_TAG_RE.sub(_stash_code, stashed)

    linked = _LINKER.linkify(stashed)
    linked = _CODE_PLACEHOLDER_RE.sub(lambda m: code_stash[int(m.group(1))], linked)
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


# Подмножество HTML, которое понимает Telegram Bot API (parse_mode=HTML):
# никаких <p>/<li>/<h3>/<img> — абзацы и списки передаются переводами строк,
# заголовки — жирным. Конвертер — на html.parser (не регулярками): иначе не
# различить нумерованный/маркированный список и вложенные списки.
_TG_INLINE_TAGS = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "blockquote"}
_TG_URL_SCHEMES = ("http://", "https://", "mailto:", "tg://")
_TG_SKIP_CONTENT = {"script", "style"}


class _TelegramHTML(HTMLParser):
    def __init__(self, origin: str | None):
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.out: list[str] = []
        self.lists: list[list] = []   # [тип "ul"/"ol", счётчик]
        self.open: list[str] = []     # открытые теги, которые переносим в вывод
        self.pre = 0
        self.skip = 0

    def _emit(self, text: str) -> None:
        self.out.append(text)

    def _ends_with_newline(self) -> bool:
        tail = "".join(self.out[-3:])
        return not tail or tail.endswith("\n")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _TG_SKIP_CONTENT:
            self.skip += 1
        elif tag in ("ul", "ol"):
            if self.lists and not self._ends_with_newline():
                self._emit("\n")  # вложенный список — с новой строки
            self.lists.append([tag, 0])
        elif tag == "li":
            depth = max(len(self.lists), 1)
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            marker = f"{kind[1]}. " if kind[0] == "ol" else "• "
            self._emit("    " * (depth - 1) + marker)
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("———\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("<b>")
            self.open.append("b")
        elif tag == "a":
            href = (attrs.get("href") or "").strip()
            if href.startswith("/") and self.origin:
                href = self.origin + href
            if href.startswith(_TG_URL_SCHEMES):
                self._emit(f'<a href="{escape(href)}">')
                self.open.append("a")
            else:
                self.open.append(None)  # ссылку без допустимой схемы — просто текстом
        elif tag == "span" and "tg-spoiler" in (attrs.get("class") or ""):
            self._emit('<span class="tg-spoiler">')
            self.open.append("span")
        elif tag in _TG_INLINE_TAGS:
            if tag == "pre":
                self.pre += 1
            self._emit(f"<{tag}>")
            self.open.append(tag)
        # остальное (p, img, div, неизвестные) — тег выбрасываем, текст оставляем

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr", "img"):
            self.handle_starttag(tag, attrs)

    def _close(self, names) -> None:
        # закрываем последний открытый из names (вместе со всем, что внутри него)
        for pos in range(len(self.open) - 1, -1, -1):
            if self.open[pos] in names or (self.open[pos] is None and "a" in names):
                for name in reversed(self.open[pos:]):
                    if name:
                        self._emit(f"</{name}>")
                del self.open[pos:]
                return

    def handle_endtag(self, tag):
        if tag in _TG_SKIP_CONTENT:
            self.skip = max(self.skip - 1, 0)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self._emit("\n")  # пустая строка после списка
        elif tag == "li":
            if not self._ends_with_newline():
                self._emit("\n")
        elif tag == "p":
            self._emit("\n" if self.lists else "\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._close({"b"})
            self._emit("\n\n")
        elif tag == "a":
            self._close({"a"})
        elif tag == "span":
            self._close({"span"})
        elif tag in _TG_INLINE_TAGS:
            self._close({tag})
            if tag == "pre":
                self.pre = max(self.pre - 1, 0)
                self._emit("\n\n")
            elif tag == "blockquote":
                self._emit("\n\n")

    def handle_data(self, data):
        if self.skip:
            return
        if not self.pre:
            # markdown с nl2br сам ставит <br> на каждый перевод строки текста,
            # а переводы строк между тегами — просто форматирование его вывода
            data = data.replace("\n", "")
            if not data:
                return
        self._emit(str(escape(data)))

    def result(self) -> str:
        self._close(set(filter(None, self.open)) | {"a"})
        text = "".join(self.out)
        text = re.sub(r"<blockquote>\s+", "<blockquote>", text)
        text = re.sub(r"\s+</blockquote>", "</blockquote>", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def telegram_html(text: str, base_url: str) -> str:
    """Markdown новости -> HTML для сообщения Telegram (см.
    telegram_bot.news_text). Сохраняется: жирный, курсив, подчёркнутый,
    ссылки, код, цитаты, спойлер ||текст|| (родной спойлер Telegram),
    заголовки (жирным), списки — маркированные «•», нумерованные «1.»,
    вложенные с отступом. Картинки выпадают (в группу они уходят альбомом),
    относительные ссылки (вложения /news/attachments/...) дополняются адресом
    сайта из base_url — Telegram относительный href отвергает."""
    _md.reset()
    pre = _SPOILER_RE.sub(lambda m: f'<span class="tg-spoiler">{escape(m.group(1))}</span>', text or "")
    html = _md.convert(pre)
    origin = re.match(r"https?://[^/]+", base_url)
    parser = _TelegramHTML(origin.group(0) if origin else None)
    parser.feed(html)
    parser.close()
    return parser.result()


_TG_SPOILER_SPAN_RE = re.compile(r'<span class="tg-spoiler">(.*?)</span>', re.S)


def max_html(text: str, base_url: str) -> str:
    """Markdown новости -> HTML для сообщения MAX (см. max_bot.news_text).
    Набор тегов MAX (b, i, u, s, code, pre, a, blockquote) совпадает с
    телеграмовским, поэтому берём telegram_html; спойлера в MAX нет —
    его текст остаётся обычным."""
    return _TG_SPOILER_SPAN_RE.sub(r"\1", telegram_html(text, base_url))
