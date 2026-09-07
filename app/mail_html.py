"""
Санитайзинг HTML-тела письма для показа в mailbox/message.html.

Тело письма — НЕДОВЕРЕННЫЙ внешний HTML (в отличие от app/news_format.py,
который чистит HTML, полученный из markdown, написанного доверенным членом
правления) — поэтому здесь отдельный, специально подобранный под почту
whitelist, и рендер идёт в песочнице (<iframe sandbox="allow-same-origin
allow-popups allow-popups-to-escape-sandbox" srcdoc="...">, см.
mailbox/message.html) как второй эшелон защиты ПОВЕРХ bleach: даже если
санитайзер что-то пропустит, sandbox не даст этому выполниться
(allow-scripts не выдан) или вырваться за пределы iframe.
allow-popups(-to-escape-sandbox) нужны специально для ссылок (см. ниже) —
без них клик по ссылке с target="_blank" внутри такого iframe просто
ничего не делает (спецификация HTML), а не открывает новую вкладку.

Внешние картинки (http/https) по умолчанию вырезаются — типичный вектор
трекинг-пикселей (сам факт загрузки картинки подтверждает отправителю, что
письмо открыто, и выдаёт IP получателя). Показываются только по явному
запросу (allow_remote_images=True, см. mailbox.view_message: ?allow_images=1).
Встроенные (cid:) картинки самого письма показываются всегда — это не
новый сетевой запрос, они уже полностью получены вместе с письмом.
Вырезаются регэкспом ДО bleach (см. _REMOTE_IMG_TAG_RE) — не через
protocols= у bleach.clean(), это раньше заодно ломало и обычные ссылки
(http/https были запрещены глобально, на любом href/src, если картинки
не показаны). Ссылки — независимо от allow_remote_images — всегда
работают и всегда принудительно открываются в новой вкладке
(target="_blank" rel="noopener noreferrer", см. _A_TAG_RE), а не
внутри iframe письма.

Инлайновый style="..." — реальные HTML-письма (в т.ч. рассылки от
SMS Aero) практически всегда свёрстаны вложенными <table> на инлайновых
стилях, без внешнего/<style>-CSS вообще; без style рушится вся вёрстка,
включая банальное центрирование — заметили именно на реальном письме.
Разрешаем его на любом теге, но не бланково: bleach.css_sanitizer.CSSSanitizer
фильтрует style по списку конкретных БЕЗОПАСНЫХ CSS-свойств (см.
_SAFE_CSS_PROPERTIES) — со своей поправкой поверх дефолтного списка bleach:
он держит "cursor", а `cursor: url(...)` — рабочий вектор трекинг-пикселя
в обход блокировки внешних <img> (проверено вручную), поэтому cursor из
списка исключён; добавлены padding/margin/border(-width/-style) — часто
встречаются в реальной вёрстке писем и не дают url()-значений в принципе.
Сам тег <style> (стили классов, а не атрибут) по-прежнему вырезается
целиком (см. _strip_head_and_rawtext_blocks) — bleach умеет чистить только
style-АТРИБУТ, а не содержимое style-ТЕГА (проверено вручную), так что
оставлять тег значило бы пропускать любой CSS, включая трекинг-пиксели
через background/cursor с url(), без всякой фильтрации.
"""
import html as html_module
import re

import bleach
from bleach.css_sanitizer import CSSSanitizer, ALLOWED_CSS_PROPERTIES

from .mail_client import MessageDetail

ALLOWED_MAIL_TAGS = [
    "p", "br", "div", "span", "a", "b", "i", "u", "strong", "em",
    "ul", "ol", "li", "blockquote", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "td", "th", "img",
]
ALLOWED_MAIL_ATTRS = {
    # style — на любом теге (см. docstring модуля), align/valign — для
    # табличной вёрстки старым, но безопасным (не CSS) способом.
    "*": ["style", "align", "valign"],
    "a": ["href", "title"],
    "img": ["src", "alt", "width", "height"],
    "table": ["width", "height", "border", "cellpadding", "cellspacing", "bgcolor", "role"],
    "td": ["colspan", "rowspan", "width", "height", "bgcolor"],
    "th": ["colspan", "rowspan", "width", "height", "bgcolor"],
}

_SAFE_CSS_PROPERTIES = sorted((ALLOWED_CSS_PROPERTIES - {"cursor"}) | {
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "border", "border-width", "border-style",
    "border-top", "border-top-width", "border-top-style",
    "border-right", "border-right-width", "border-right-style",
    "border-bottom", "border-bottom-width", "border-bottom-style",
    "border-left", "border-left-width", "border-left-style",
})
_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=_SAFE_CSS_PROPERTIES)

_CID_RE = re.compile(r'src=(["\'])cid:([^"\']+)\1')
_REMOTE_IMG_RE = re.compile(r'<img\b[^>]*\bsrc=["\']https?://', re.IGNORECASE)
# Тот же признак, что и _REMOTE_IMG_RE (только для показа плашки "картинки
# скрыты"), но с захватом всего тега целиком — для реального вырезания
# (см. render_email_body: раньше внешние картинки блокировались побочным
# эффектом bleach(protocols=...) — не пропускать http/https вообще, если
# allow_remote_images=False, — но это заодно ломало и обычные ссылки
# <a href="https://...">, которые должны работать всегда, независимо от
# показа картинок; теперь протоколы http/https разрешены всегда, а внешние
# картинки вырезаются этим регэкспом отдельно, ДО bleach).
_REMOTE_IMG_TAG_RE = re.compile(r'<img\b[^>]*\bsrc=["\']https?://[^"\']*["\'][^>]*>', re.IGNORECASE)
# Ссылки открываются в новой вкладке, а не внутри iframe письма (там и так
# нет allow-top-navigation, поэтому клик по <a> без target либо открыл бы
# письмо поверх самого себя внутри песочницы, либо — после этого изменения
# — просто ничего не делал бы без sandbox="allow-popups" на iframe, см.
# mailbox/message.html). Применяется ПОСЛЕ bleach.clean(), когда html уже
# полностью санитайзирован и его структуру целиком контролирует сериализатор
# bleach (весь текст — гарантированно "<a " с пробелом сразу после), а не
# исходное письмо — поэтому здесь безопасен простой regex-replace, в
# отличие от разбора недоверенного html выше.
_A_TAG_RE = re.compile(r"<a\s", re.IGNORECASE)
# html5lib (парсер, на котором работает bleach) токенизирует содержимое
# <style>/<script>/<title> (и ряда более редких тегов вроде <textarea>) как
# raw/RCDATA-text уже на этапе парсинга — фиксированное правило HTML5, не
# зависящее от whitelist тегов. Поэтому bleach.clean(..., strip=True), не
# найдя эти теги в ALLOWED_MAIL_TAGS, вырезает сам тег, но ОСТАВЛЯЕТ его
# текстовое содержимое как обычный видимый текст письма — реальный случай:
# письмо от SMS Aero показывало и исходный CSS из <style>, и заголовок
# страницы из <title> открытым текстом в начале письма. <head> целиком не
# предназначен для показа в теле письма в принципе (мета-теги, title,
# стили, условные комментарии для Outlook) — вырезаем его одним куском;
# <style>/<script>/<title> отдельно — на случай, если разметка письма
# рваная и такой тег затесался вне <head> (в реальной почте случается).
_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_RAWTEXT_TAG_RE = re.compile(r"<(style|script|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


def _strip_head_and_rawtext_blocks(html: str) -> str:
    html = _HEAD_RE.sub("", html)
    return _RAWTEXT_TAG_RE.sub("", html)


def _substitute_cid_images(html: str, inline_images: dict[str, tuple[object, bytes]]) -> str:
    """src="cid:xxx" -> src="data:<mime>;base64,..." — делается ДО bleach,
    чтобы data: спокойно прошла через whitelist протоколов ниже вместе с
    остальными разрешёнными src."""
    import base64

    def repl(match: re.Match) -> str:
        quote, cid = match.group(1), match.group(2)
        found = inline_images.get(cid)
        if found is None:
            return match.group(0)
        part, data = found
        encoded = base64.b64encode(data).decode("ascii")
        return f'src={quote}data:{part.content_type};base64,{encoded}{quote}'

    return _CID_RE.sub(repl, html)


def _wrap_html_document(body_html: str) -> str:
    return f'<!doctype html><html><head><meta charset="utf-8"></head><body>{body_html}</body></html>'


def render_email_body(detail: MessageDetail, allow_remote_images: bool) -> tuple[str, bool]:
    """Возвращает (html_для_srcdoc, had_blocked_images).

    html_для_srcdoc — ОБЫЧНАЯ str, НЕ Markup/|safe. Подставлять в шаблон
    ТОЛЬКО в контекст HTML-атрибута (srcdoc="{{ ... }}"), полагаясь на
    автоэкранирование Jinja — если завернуть в Markup, Jinja перестанет
    экранировать кавычки/спецсимволы письма, атрибут srcdoc разорвётся
    посреди значения, и это будет инъекция уже в саму страницу-обёртку
    (не в песочницу iframe, а в её DOM-родителя) — НЕ повторять эту ошибку
    при рефакторинге.
    """
    if detail.body_html is not None:
        raw_html = detail.body_html
        had_blocked = bool(_REMOTE_IMG_RE.search(raw_html)) if not allow_remote_images else False
        if not allow_remote_images:
            raw_html = _REMOTE_IMG_TAG_RE.sub("", raw_html)
        raw_html = _substitute_cid_images(raw_html, detail.inline_images)
        raw_html = _strip_head_and_rawtext_blocks(raw_html)
    else:
        raw_html = f"<pre>{html_module.escape(detail.body_text or '')}</pre>"
        had_blocked = False

    # http/https разрешены всегда (для обычных ссылок <a href> — см.
    # _A_TAG_RE выше) — внешние картинки вырезаны отдельно уже выше, ДО
    # bleach, не через protocols (иначе это ломало бы и ссылки заодно).
    clean = bleach.clean(
        raw_html, tags=ALLOWED_MAIL_TAGS, attributes=ALLOWED_MAIL_ATTRS,
        css_sanitizer=_CSS_SANITIZER,
        protocols=["data", "mailto", "http", "https"], strip=True, strip_comments=True,
    )
    clean = _A_TAG_RE.sub('<a target="_blank" rel="noopener noreferrer" ', clean)
    return _wrap_html_document(clean), had_blocked
