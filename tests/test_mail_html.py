"""
Тесты санитайзинга HTML-тела писем (app/mail_html.py) — тело письма это
недоверенный внешний HTML, в отличие от app/news_format.py (markdown от
доверенного члена правления). Покрывает: вырезание script/event-handler/
javascript:-схемы, блокировку внешних картинок по умолчанию и их показ по
запросу, подстановку inline (cid:) картинок как data: URI (показываются
всегда — это не новый сетевой запрос), и регресс на конкретную ошибку —
srcdoc не должен быть обёрнут в Markup (иначе Jinja перестанет его
экранировать и HTML-атрибут в шаблоне будет разорван содержимым письма).
"""
from markupsafe import Markup

from app.mail_html import render_email_body
from app.mail_client import MessageDetail, AttachmentPart


def _detail(body_html=None, body_text=None, inline_images=None):
    return MessageDetail(
        uid="1", subject="t", from_name=None, from_addr=None, to_addrs=[], date=None,
        body_text=body_text, body_html=body_html, attachments=[],
        inline_images=inline_images or {},
    )


def test_script_tag_is_stripped():
    html, _ = render_email_body(_detail(body_html="<p>hi</p><script>alert(1)</script>"), allow_remote_images=False)
    assert "<script" not in html
    assert "alert(1)" not in html or "<script" not in html  # текст может остаться, тег — нет


def test_event_handler_attribute_is_stripped():
    html, _ = render_email_body(_detail(body_html='<img src="x" onerror="alert(1)">'), allow_remote_images=True)
    assert "onerror" not in html


def test_javascript_href_is_stripped():
    html, _ = render_email_body(_detail(body_html='<a href="javascript:alert(1)">click</a>'), allow_remote_images=False)
    assert "javascript:" not in html


def test_remote_image_blocked_by_default():
    html, had_blocked = render_email_body(_detail(body_html='<img src="https://tracker.example/pixel.gif">'), allow_remote_images=False)
    assert "tracker.example" not in html
    assert had_blocked is True


def test_remote_image_shown_when_allowed():
    html, had_blocked = render_email_body(_detail(body_html='<img src="https://example.com/logo.png">'), allow_remote_images=True)
    assert "example.com/logo.png" in html
    assert had_blocked is False


def test_inline_cid_image_always_shown_regardless_of_allow_remote_images():
    att = AttachmentPart(index=1, filename="logo.png", content_type="image/png", size=3)
    detail = _detail(body_html="<img src='cid:logo1'>", inline_images={"logo1": (att, b"\x89PNG")})
    html, had_blocked = render_email_body(detail, allow_remote_images=False)
    assert "data:image/png;base64" in html
    assert had_blocked is False  # inline-картинка не в счёт «внешних заблокированных»


def test_plain_text_only_message_is_escaped_and_wrapped():
    html, had_blocked = render_email_body(_detail(body_html=None, body_text="<b>не тег</b> & спецсимволы"), allow_remote_images=False)
    assert "&lt;b&gt;" in html
    assert "<b>не тег</b>" not in html
    assert had_blocked is False


def test_srcdoc_output_is_plain_str_not_markup():
    """Регресс: если html_для_srcdoc окажется Markup, Jinja перестанет его
    экранировать при вставке в атрибут srcdoc="{{ ... }}" — письмо с
    кавычками разорвёт сам HTML-атрибут страницы (инъекция в DOM-родителя,
    не в песочницу iframe)."""
    html, _ = render_email_body(_detail(body_html='<p>text with "quotes" and \'apostrophes\'</p>'), allow_remote_images=False)
    assert not isinstance(html, Markup)
    assert isinstance(html, str)


def test_disallowed_tags_stripped_but_content_kept():
    html, _ = render_email_body(_detail(body_html="<style>body{color:red}</style><p>視覚的</p>"), allow_remote_images=False)
    assert "<style" not in html
    assert "視覚的" in html


def test_table_structure_is_allowed():
    html, _ = render_email_body(_detail(body_html="<table><tr><td>A</td><th>B</th></tr></table>"), allow_remote_images=False)
    assert "<table>" in html and "<td>A</td>" in html and "<th>B</th>" in html
