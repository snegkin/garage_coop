"""
Санитайзинг HTML-тела письма для показа в mailbox/message.html.

Тело письма — НЕДОВЕРЕННЫЙ внешний HTML (в отличие от app/news_format.py,
который чистит HTML, полученный из markdown, написанного доверенным членом
правления) — поэтому здесь отдельный, специально подобранный под почту
whitelist, и рендер идёт в песочнице (<iframe sandbox="" srcdoc="...">,
см. mailbox/message.html) как второй эшелон защиты ПОВЕРХ bleach: даже
если санитайзер что-то пропустит, sandbox не даст этому выполниться или
вырваться за пределы iframe.

Внешние картинки (http/https) по умолчанию вырезаются — типичный вектор
трекинг-пикселей (сам факт загрузки картинки подтверждает отправителю, что
письмо открыто, и выдаёт IP получателя). Показываются только по явному
запросу (allow_remote_images=True, см. mailbox.view_message: ?allow_images=1).
Встроенные (cid:) картинки самого письма показываются всегда — это не
новый сетевой запрос, они уже полностью получены вместе с письмом.
"""
import html as html_module
import re

import bleach

from .mail_client import MessageDetail

ALLOWED_MAIL_TAGS = [
    "p", "br", "div", "span", "a", "b", "i", "u", "strong", "em",
    "ul", "ol", "li", "blockquote", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "td", "th", "img",
]
ALLOWED_MAIL_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "width", "height"],  # намеренно без style/class — не даём вектор CSS-инъекции/фингерпринтинга через атрибуты
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}

_CID_RE = re.compile(r'src=(["\'])cid:([^"\']+)\1')
_REMOTE_IMG_RE = re.compile(r'<img\b[^>]*\bsrc=["\']https?://', re.IGNORECASE)


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
        raw_html = _substitute_cid_images(raw_html, detail.inline_images)
    else:
        raw_html = f"<pre>{html_module.escape(detail.body_text or '')}</pre>"
        had_blocked = False

    protocols = ["data", "mailto"] + (["http", "https"] if allow_remote_images else [])
    clean = bleach.clean(
        raw_html, tags=ALLOWED_MAIL_TAGS, attributes=ALLOWED_MAIL_ATTRS,
        protocols=protocols, strip=True, strip_comments=True,
    )
    return _wrap_html_document(clean), had_blocked
