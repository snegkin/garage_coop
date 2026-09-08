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


def test_spoiler_url_is_not_auto_linked():
    """Регресс: bleach.linkify() сам находил URL внутри ||...|| и
    оборачивал его в <a> — у ссылки свой цвет (a{color:...} в base.html
    побеждает унаследованный от .wiki-spoiler), маскировка «текст того же
    цвета, что фон» переставала работать, спрятанный URL был виден как
    обычная ссылка. Внутри спойлера URL должен остаться простым текстом."""
    from app.news_format import render_html
    html = str(render_html("||https://example.com/secret-page||"))
    assert "<a " not in html
    assert "https://example.com/secret-page" in html


def test_url_outside_spoiler_is_still_auto_linked():
    """Тот же linkify не должен перестать работать для обычных ссылок —
    плейсхолдерами прячутся только уже собранные .wiki-spoiler спаны,
    остальной текст проходит через linkify как обычно."""
    from app.news_format import render_html
    html = str(render_html("Смотрите https://example.com/public — открыто всем."))
    assert '<a href="https://example.com/public"' in html


def test_punycode_domain_is_linkified_whole_not_cut_at_xn():
    """Регресс: встроенный список доменных зон bleach (bleach.linkifier.TLDS)
    по ошибке содержит "xn" отдельной зоной (это ACE/punycode-префикс
    "xn--", не сама зона) — из-за этого punycode-домен (например,
    кириллический ".рф" в punycode) обрывался ровно на "xn":
    "xn----dtbbg1boax0b.xn--p1ai" превращалось в ссылку
    "xn----dtbbg1boax0b.xn", а "--p1ai" оставалось обычным текстом рядом.
    См. app/news_format.py: _TLDS_WITH_PUNYCODE."""
    from app.news_format import render_html
    html = str(render_html("см. xn----dtbbg1boax0b.xn--p1ai пример"))
    assert '<a href="http://xn----dtbbg1boax0b.xn--p1ai"' in html
    assert ">xn----dtbbg1boax0b.xn--p1ai</a> пример" in html  # весь домен внутри ссылки, "--p1ai" не остаётся снаружи


def test_inline_code_is_not_auto_linked():
    """Реальный случай: "Login: pravlenie@xn----dtbbg1boax0b.xn--p1ai" —
    punycode-домен вида "xn----..." сразу после "@" линкуется bleach НЕ
    целиком (даже с починенным списком TLD выше) — ссылка начинается с
    середины слова, сразу после "xn": "...@xn" остаётся текстом. Общий
    выход для автора статьи — обернуть техническую строку в код, внутри
    него ссылки не ищутся вообще (см. app/news_format.py: _CODE_TAG_RE)."""
    from app.news_format import render_html
    html = str(render_html("Login: `pravlenie@xn----dtbbg1boax0b.xn--p1ai`"))
    assert "<a " not in html
    assert "<code>pravlenie@xn----dtbbg1boax0b.xn--p1ai</code>" in html


def test_code_block_url_is_not_auto_linked():
    """Тот же принцип для отступного/```-блока кода (<pre><code>...), не
    только для инлайн `кода`."""
    from app.news_format import render_html
    html = str(render_html("    https://example.com/in-code-block\n"))
    assert "<a " not in html
    assert "<pre><code>https://example.com/in-code-block" in html


def test_url_outside_code_is_still_auto_linked():
    """Стэш кода не должен ломать linkify для обычного текста рядом."""
    from app.news_format import render_html
    html = str(render_html("`login123` смотрите https://example.com/public"))
    assert "<code>login123</code>" in html
    assert '<a href="https://example.com/public"' in html


# ---------------------------------------------------------------------------
# Кнопка "Скопировать" и сворачивание длинного блока кода (см.
# app/news_format.py: _wrap_long_code_block, CODE_BLOCK_COLLAPSE_LINES) —
# включено только у вики (render_wiki_html/wiki.py:preview), НЕ у новостей
# и не у почты (обе кнопки не работают без JS в письме).
# ---------------------------------------------------------------------------

def _indented_code(n_lines: int) -> str:
    return "\n".join(f"    line{i}" for i in range(1, n_lines + 1)) + "\n"


def test_render_html_default_does_not_wrap_code_block():
    from app.news_format import render_html
    html = str(render_html(_indented_code(20)))
    assert "wiki-code-block" not in html
    assert "wiki-copy-btn" not in html
    assert "<pre><code>" in html


def test_render_html_collapses_long_code_block_when_enabled(db):
    """collapse_long_code=True зовёт _() (app/i18n.py: translate), а тому
    нужен g.locale — отсюда фикстура db (только ради app_context, сама БД
    здесь не нужна)."""
    from app.news_format import render_html, CODE_BLOCK_COLLAPSE_LINES
    html = str(render_html(_indented_code(CODE_BLOCK_COLLAPSE_LINES + 5), collapse_long_code=True))
    assert 'class="wiki-code-block is-collapsed"' in html
    assert "wiki-code-toggle" in html
    assert "wiki-copy-btn" in html
    assert f"({CODE_BLOCK_COLLAPSE_LINES + 5} строк)" in html
    assert "line1" in html and f"line{CODE_BLOCK_COLLAPSE_LINES + 5}" in html  # содержимое никуда не делось, только свёрнуто CSS


def test_render_html_wraps_short_code_block_with_copy_button_but_not_collapsed(db):
    """Короткий блок при collapse_long_code=True тоже оборачивается — ради
    кнопки "Скопировать" — но БЕЗ is-collapsed и без кнопки разворачивания:
    сворачивать короткий блок незачем."""
    from app.news_format import render_html, CODE_BLOCK_COLLAPSE_LINES
    html = str(render_html(_indented_code(CODE_BLOCK_COLLAPSE_LINES), collapse_long_code=True))
    assert 'class="wiki-code-block"' in html
    assert "is-collapsed" not in html
    assert "wiki-code-toggle" not in html
    assert "wiki-copy-btn" in html


def test_wiki_preview_collapses_long_code_block(db, client):
    """Предпросмотр из тулбара формы должен вести себя так же, как
    сохранённая страница (см. test_wiki_preview_uses_same_renderer_as_news) —
    в т.ч. сворачивать длинный блок кода."""
    from app.news_format import CODE_BLOCK_COLLAPSE_LINES
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/wiki/preview", data={"body": _indented_code(CODE_BLOCK_COLLAPSE_LINES + 1)})
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert "wiki-code-block" in html


def test_news_preview_does_not_collapse_long_code_block(db, client):
    """Новости — короче, кнопка сворачивания там не нужна (см. docstring
    render_html в app/news_format.py)."""
    from app.news_format import CODE_BLOCK_COLLAPSE_LINES
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/preview", data={"body": _indented_code(CODE_BLOCK_COLLAPSE_LINES + 1)})
    assert resp.status_code == 200
    html = resp.get_json()["html"]
    assert "wiki-code-block" not in html
