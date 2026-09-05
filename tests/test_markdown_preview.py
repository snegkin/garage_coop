"""
Предпросмотр markdown из формы новости/страницы вики (app/news.py:preview,
app/wiki.py:preview) — AJAX-эндпоинт, рендерит ТЕКУЩИЙ текст textarea (ещё
не сохранённый) тем же render_html(), что и опубликованная статья/
страница — не отдельный JS-рендерер markdown, чтобы предпросмотр не мог
разойтись с настоящим выводом.
"""
from app.models import RoleEnum

from tests.conftest import make_person, make_user, login


def _board_user(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def _member_user(db, username="member1"):
    person = make_person(db, full_name="Member One")
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()


# ---------------------------------------------------------------------------
# Новости
# ---------------------------------------------------------------------------

def test_news_preview_renders_markdown(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/preview", data={"body": "**жирный** и *курсив*"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "<strong>жирный</strong>" in data["html"]
    assert "<em>курсив</em>" in data["html"]


def test_news_preview_sanitizes_script_tags(db, client):
    """render_html() уже прогоняет через bleach — предпросмотр не должен
    внезапно отдавать неэкранированный script, даже если markdown-парсер
    пропустит сырой HTML в тексте."""
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/preview", data={"body": "<script>alert(1)</script>текст"})
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert "<script>" not in html


def test_news_preview_handles_empty_body(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/preview", data={})
    assert resp.status_code == 200
    assert resp.get_json()["html"] == ""


def test_news_preview_requires_board(db, client):
    _member_user(db)
    login(client, "member1", "pass1234")

    resp = client.post("/news/preview", data={"body": "текст"})
    assert resp.status_code == 302  # roles_required редиректит, не 403


def test_news_preview_requires_login(client):
    resp = client.post("/news/preview", data={"body": "текст"})
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Вики
# ---------------------------------------------------------------------------

def test_wiki_preview_renders_markdown(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/wiki/preview", data={"body": "### Заголовок\n\n- пункт 1\n- пункт 2"})
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert "<h3>Заголовок</h3>" in html
    assert "<li>пункт 1</li>" in html


def test_wiki_preview_requires_board(db, client):
    _member_user(db)
    login(client, "member1", "pass1234")

    resp = client.post("/wiki/preview", data={"body": "текст"})
    assert resp.status_code == 302


def test_wiki_preview_uses_same_renderer_as_news():
    """Вики переиспользует ровно тот же render_html, что и новости (см.
    app/wiki.py: from .news_format import render_html) — не отдельная копия
    markdown-логики, которая могла бы незаметно разойтись."""
    from app.news import render_html as news_render_html
    from app.wiki import render_html as wiki_render_html
    assert news_render_html is wiki_render_html


# ---------------------------------------------------------------------------
# Скрытый текст ||...|| (см. app/news_format.py: _SPOILER_RE) — раскрывается
# по клику через JS в base.html (.wiki-spoiler), не разметка доступа: текст
# всё равно есть в HTML страницы, просто визуально скрыт по умолчанию.
# ---------------------------------------------------------------------------

def test_wiki_preview_renders_spoiler_span(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/wiki/preview", data={"body": "Пароль от роутера: ||admin123||"})
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert '<span class="wiki-spoiler" tabindex="0" role="button">admin123</span>' in html


def test_news_preview_renders_spoiler_span(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/preview", data={"body": "||секрет||"})
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert '<span class="wiki-spoiler"' in html
    assert "секрет" in html


def test_spoiler_content_is_html_escaped():
    """Даже если внутри ||...|| оказались символы разметки — они не должны
    сломать структуру страницы (экранируются перед подстановкой в span, см.
    news_format._spoiler_sub)."""
    from app.news_format import render_html
    html = str(render_html("||<b>bold</b> and \"quotes\"||"))
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;" in html


def test_spoiler_does_not_bypass_script_sanitization():
    from app.news_format import render_html
    html = str(render_html("||<script>alert(1)</script>||"))
    assert "<script>" not in html


def test_multiple_spoilers_on_same_line_render_separately():
    from app.news_format import render_html
    html = str(render_html("логин ||admin|| пароль ||secret123||"))
    assert html.count('class="wiki-spoiler"') == 2
    assert "admin" in html and "secret123" in html
