"""
Разметка сообщений чатов (правления — app/board_chat.py, ревизионной
комиссии — app/revision_chat.py): текст хранится как есть, с разметкой в
стиле Telegram Desktop, и превращается в HTML при показе на сайте и при
отправке в группы Telegram/MAX (оба принимают HTML: parse_mode=HTML /
"format": "html").

Разметка (её же расставляют горячие клавиши поля ввода, см. base.html):
  **жирный**  __курсив__  ++подчёркнутый++  ~~зачёркнутый~~
  `код`  ```блок кода```  [текст](https://ссылка)
Голые адреса (https://…, www.…, а также пример.рф и прочие кириллические
зоны без https://) — тоже ссылки.

Кириллические домены: в href (сайт, Telegram, MAX) — всегда punycode
(xn--…), показывается — кириллицей, и %D0%… в пути — буквами
(display_url); как адрес написан — кириллицей или punycode — не важно.
Часть домена со смесью латиницы и другого алфавита (аррle.com с русскими
«р») остаётся punycode — как в браузерах, против подделки адресов.

Маркер открывает выделение, только если за ним не пробел, закрывает — если
перед ним не пробел; маркер без пары — обычный текст («C++ и C++» остаётся
как написан). У __ вдобавок рядом снаружи не должно быть буквы/цифры —
иначе курсивом становился бы snake__case__name; остальные маркеры, как в
Telegram, работают и внутри слова (при**ВЕТ**).

Обратно — from_telegram/from_max: текст из группы с форматированием
(entities Telegram, markup MAX) переводится в эту же разметку.
"""
import re

from markupsafe import escape

# Порядок важен: ``` раньше `.
_SPANS = (("**", "b"), ("__", "i"), ("++", "u"), ("~~", "s"))
_SPAN_TAGS = dict(_SPANS)

# Кириллические зоны, которые узнаются и без https:// (как в Telegram).
_IDN_TLDS = r"(?:рф|рус|москва|онлайн|сайт|орг|ком|дети|бел|укр|xn--[a-z0-9-]+)"
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+"
    r"|[a-z0-9а-яё-]+(?:\.[a-z0-9а-яё-]+)*\." + _IDN_TLDS + r"(?![a-z0-9а-яё-])(?:[/?#:][^\s<>\"']*)?",
    re.IGNORECASE)
_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(([^()\s]+)\)")
_TRAILING_PUNCT = ".,;:!?…»\"'"


def _safe_url(url: str) -> str | None:
    """Адрес ссылки — только http(s)/mailto/tg; без схемы — https://.
    Иное (javascript: и т. п.) — None, ссылка остаётся текстом."""
    url = url.strip()
    if not url:
        return None
    if re.match(r"^(https?://|mailto:|tg://)", url, re.IGNORECASE):
        return url
    if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
        return None
    if re.match(r"^[\w.-]+\.(?:[a-zа-яё]{2,}|xn--[a-z0-9-]+)(?:[/?#:]|$)", url, re.IGNORECASE):
        return "https://" + url
    return None


def _split_url(url: str):
    """-> (схема с //, userinfo@, хост, :порт, остальное) или None."""
    m = re.match(r"^([a-z][a-z0-9+.-]*://)([^/?#]*)(.*)$", url, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    scheme, netloc, rest = m.groups()
    userinfo, _, hostport = netloc.rpartition("@")
    host, port = hostport, ""
    pm = re.match(r"^(.*?)(:\d*)$", hostport)
    if pm and not hostport.startswith("["):
        host, port = pm.groups()
    return scheme, (userinfo + "@" if userinfo else ""), host, port, rest


def _mixed_script(label: str) -> bool:
    latin = any("a" <= c.lower() <= "z" for c in label)
    other = any(c.isalpha() and not c.isascii() for c in label)
    return latin and other


def _host_to_ascii(host: str) -> str:
    labels = []
    for label in host.split("."):
        if label.isascii():
            labels.append(label)
            continue
        try:
            labels.append(label.encode("idna").decode("ascii"))
        except UnicodeError:
            return host
    return ".".join(labels)


def _host_to_unicode(host: str) -> str:
    labels = []
    for label in host.split("."):
        if label.lower().startswith("xn--"):
            try:
                decoded = label.encode("ascii").decode("idna")
            except UnicodeError:
                decoded = label
            labels.append(label if _mixed_script(decoded) else decoded)
        elif not label.isascii() and _mixed_script(label):
            labels.append(_host_to_ascii(label))
        else:
            labels.append(label)
    return ".".join(labels)


_PCT_RUN_RE = re.compile(r"(?:%[89A-Fa-f][0-9A-Fa-f])+")


def _unquote_non_ascii(rest: str) -> str:
    """%D0%BF… -> буквы; только не-ASCII байты (UTF-8), %2F/%20 и т. п. —
    как есть, чтобы смысл адреса не менялся."""
    def repl(m):
        try:
            text = bytes(int(h, 16) for h in m.group(0)[1:].split("%")).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return m.group(0)
        return m.group(0) if any(not c.isprintable() or c.isspace() for c in text) else text
    return _PCT_RUN_RE.sub(repl, rest)


def href_url(url: str) -> str:
    """Адрес для href — ASCII: домен в punycode, не-ASCII в пути —
    %-кодами (так его примут и Telegram, и MAX, и любой браузер)."""
    parts = _split_url(url)
    if parts is None:
        return url
    scheme, userinfo, host, port, rest = parts
    rest = "".join(c if c.isascii() else "".join(f"%{b:02X}" for b in c.encode("utf-8")) for c in rest)
    return scheme + userinfo + _host_to_ascii(host) + port + rest


def display_url(url: str) -> str:
    """Адрес для показа — домен кириллицей, %-коды букв — буквами."""
    parts = _split_url(url)
    if parts is None:
        return url
    scheme, userinfo, host, port, rest = parts
    return scheme + userinfo + _host_to_unicode(host) + port + _unquote_non_ascii(rest)


def _url_key(url: str) -> str:
    """Для сравнения «текст ссылки — это тот же адрес»."""
    key = href_url(url).lower()
    return key + "/" if re.match(r"^[a-z][a-z0-9+.-]*://[^/?#]*$", key) else key


def _bare_url(s: str, i: int) -> str | None:
    if i > 0 and (s[i - 1].isalnum() or s[i - 1] in "/@"):
        return None
    m = _URL_RE.match(s, i)
    if not m:
        return None
    url = m.group(0)
    # Хвостовая пунктуация — не часть адреса; закрывающая скобка — только
    # если в адресе нет открывающей (https://ru.wikipedia.org/wiki/X_(Y)).
    while url and (url[-1] in _TRAILING_PUNCT or (url[-1] == ")" and url.count("(") < url.count(")"))):
        url = url[:-1]
    if url.lower() in ("http://", "https://", "www.") or len(url) < 5:
        return None
    return url


def _can_open(s: str, i: int, marker: str) -> bool:
    after = i + len(marker)
    if after >= len(s) or s[after].isspace():
        return False
    return marker != "__" or i == 0 or not s[i - 1].isalnum()


def _can_close(s: str, i: int, marker: str) -> bool:
    if i == 0 or s[i - 1].isspace():
        return False
    after = i + len(marker)
    return marker != "__" or after >= len(s) or not s[after].isalnum()


def _parse(s: str, i: int = 0, closer: str | None = None, depth: int = 0):
    """-> (узлы, позиция, закрыт ли closer). Узел — str, ("b"/"i"/"u"/"s",
    дети), ("code"/"pre", текст), ("a", url, дети)."""
    nodes: list = []
    buf: list[str] = []

    def flush():
        if buf:
            nodes.append("".join(buf))
            buf.clear()

    n = len(s)
    while i < n:
        if closer and s.startswith(closer, i) and _can_close(s, i, closer):
            flush()
            return nodes, i, True
        ch = s[i]
        if s.startswith("```", i):
            end = s.find("```", i + 3)
            if end > i + 3:
                flush()
                code = s[i + 3:end]
                if code.startswith("\n"):
                    code = code[1:]
                if code.endswith("\n"):
                    code = code[:-1]
                nodes.append(("pre", code))
                i = end + 3
                continue
        elif ch == "`":
            end = s.find("`", i + 1)
            if end > i + 1 and "\n" not in s[i + 1:end]:
                flush()
                nodes.append(("code", s[i + 1:end]))
                i = end + 1
                continue
        elif ch == "[":
            m = _LINK_RE.match(s, i)
            url = _safe_url(m.group(2)) if m else None
            if url:
                flush()
                label = m.group(1)
                link_text = _safe_url(label)
                if link_text and _url_key(link_text) == _url_key(url):
                    # текст — тот же адрес (в другой записи) — показать читаемо
                    children = [display_url(url)]
                else:
                    children, _, _ = _parse(label, depth=depth + 1)
                nodes.append(("a", url, children))
                i = m.end()
                continue
        elif ch.isalnum():
            url = _bare_url(s, i)
            if url:
                safe = _safe_url(url)
                if safe:
                    flush()
                    # как написан (без https:// — так и показываем), но
                    # домен кириллицей и %-коды буквами
                    shown = display_url(safe)
                    if safe != url and shown.startswith("https://"):
                        shown = shown[len("https://"):]
                    nodes.append(("a", safe, [shown]))
                    i += len(url)
                    continue
        if depth < 8:
            matched = False
            for marker, tag in _SPANS:
                if marker != closer and s.startswith(marker, i) and _can_open(s, i, marker):
                    children, j, closed = _parse(s, i + len(marker), marker, depth + 1)
                    if closed and children:
                        flush()
                        nodes.append((tag, children))
                        i = j + len(marker)
                        matched = True
                    break
            if matched:
                continue
            # маркер без пары — обычный текст целиком, не по символу
            marker = next((m for m, _ in _SPANS if s.startswith(m, i)), None)
            if marker:
                buf.append(marker)
                i += len(marker)
                continue
        buf.append(ch)
        i += 1
    flush()
    return nodes, i, False


def parse(text: str) -> list:
    return _parse(text or "")[0]


def _html(nodes, site: bool) -> str:
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(str(escape(node)))
        elif node[0] == "a":
            attrs = f' href="{escape(href_url(node[1]))}"'
            if site:
                attrs += ' target="_blank" rel="noopener noreferrer nofollow"'
            out.append(f"<a{attrs}>{_html(node[2], site)}</a>")
        elif node[0] in ("code", "pre"):
            out.append(f"<{node[0]}>{escape(node[1])}</{node[0]}>")
        else:
            out.append(f"<{node[0]}>{_html(node[1], site)}</{node[0]}>")
    return "".join(out)


def to_html(text: str) -> str:
    """HTML для показа на сайте (ссылки — в новой вкладке). Безопасен для
    innerHTML: весь текст экранирован, теги — только свои."""
    return _html(parse(text), site=True)


def to_messenger_html(text: str) -> str:
    """HTML для Telegram (parse_mode=HTML) и MAX ("format": "html"): теги
    b/i/u/s/code/pre/a поддерживают оба."""
    return _html(parse(text), site=False)


def _plain(nodes) -> str:
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif node[0] == "a":
            out.append(_plain(node[2]))
        elif node[0] in ("code", "pre"):
            out.append(node[1])
        else:
            out.append(_plain(node[1]))
    return "".join(out)


def to_plain(text: str) -> str:
    """Текст без разметки — для цитат, закрепа, уведомлений."""
    return _plain(parse(text))


# ---------------------------------------------------------------------------
# Из мессенджеров
# ---------------------------------------------------------------------------

_TELEGRAM_TYPES = {"bold": "**", "italic": "__", "underline": "++", "strikethrough": "~~",
                   "code": "`", "pre": "```"}
_MAX_TYPES = {"strong": "**", "emphasized": "__", "underline": "++", "strikethrough": "~~",
              "monospaced": "`"}


def _apply(text: str, spans) -> str:
    """spans — [(start, length, open, close)] в единицах UTF-16 (так
    считают смещения и Telegram, и MAX). Края выделения без пробелов
    (иначе маркер не сработает, см. _can_open/_can_close); выделения внутри
    кода пропускаются."""
    units = text.encode("utf-16-le")
    total = len(units) // 2

    def unit(k):
        return units[2 * k:2 * k + 2].decode("utf-16-le", errors="ignore")

    events = []
    code_ranges = []
    for start, length, opener, closer in sorted(spans, key=lambda x: (x[0], -x[1])):
        end = min(start + length, total)
        start = max(start, 0)
        if opener not in ("```",):
            while start < end and unit(start).isspace():
                start += 1
            while end > start and unit(end - 1).isspace():
                end -= 1
        if end <= start:
            continue
        if any(a <= start and end <= b for a, b in code_ranges):
            continue
        if opener in ("`", "```"):
            code_ranges.append((start, end))
            if opener == "```":
                opener, closer = "```\n", "\n```"
        events.append((start, 1, -end, opener))
        events.append((end, 0, -start, closer))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    out, pos = [], 0
    for at, _, _, marker in events:
        out.append(units[2 * pos:2 * at].decode("utf-16-le", errors="ignore"))
        out.append(marker)
        pos = at
    out.append(units[2 * pos:].decode("utf-16-le", errors="ignore"))
    return "".join(out)


def from_telegram(text: str, entities: list | None) -> str:
    """Текст сообщения Telegram + entities (или caption + caption_entities)
    -> текст с разметкой. Голые ссылки (url) и упоминания — как есть."""
    spans = []
    for e in entities or []:
        kind = e.get("type")
        start, length = e.get("offset", 0), e.get("length", 0)
        if kind == "text_link" and _safe_url(e.get("url") or ""):
            spans.append((start, length, "[", f"]({e['url']})"))
        elif kind in _TELEGRAM_TYPES:
            spans.append((start, length, _TELEGRAM_TYPES[kind], _TELEGRAM_TYPES[kind]))
    return _apply(text or "", spans) if spans else (text or "")


def from_max(text: str, markup: list | None) -> str:
    """Текст сообщения MAX + body.markup -> текст с разметкой."""
    spans = []
    for m in markup or []:
        kind = m.get("type")
        start, length = m.get("from", 0), m.get("length", 0)
        if kind == "link" and _safe_url(m.get("url") or ""):
            spans.append((start, length, "[", f"]({m['url']})"))
        elif kind in _MAX_TYPES:
            spans.append((start, length, _MAX_TYPES[kind], _MAX_TYPES[kind]))
    return _apply(text or "", spans) if spans else (text or "")
