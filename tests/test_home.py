"""
Главная страница сайта (`/`, app/main.py: index()) — общедоступна и сама
по себе (по прямому переходу) отдаёт 200 для ЛЮБОЙ роли без редиректов.
Показывает новости (news.latest_news()), компактное превью
видеонаблюдения (surveillance.recorders_with_combined_snapshots()) и
последние объявления с доски (bulletin.latest_posts() — те же правила
видимости «только для членов», что и в /bulletin/).

Но это НЕ единственная "главная": для правления/председателя рабочей
главной остаётся дашборд (по прямой просьбе) — лого в шапке и вход по
умолчанию (без явного next) для них ведут на /dashboard, а не на /;
для рядовых членов и анонимных — на / (см. auth._complete_login,
tests ниже).
"""
from app.models import RoleEnum, News, BulletinPost, BulletinCategory

from tests.conftest import make_user, login


def test_home_accessible_to_anonymous(db, client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_home_accessible_to_logged_in_member(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get("/")
    assert resp.status_code == 200


def test_home_shows_news(db, client):
    db.add(News(title="Собрание перенесено", body="Текст новости"))
    db.commit()

    body = client.get("/").get_data(as_text=True)
    assert "Собрание перенесено" in body


def test_home_renders_full_formatting_not_flattened_excerpt(db, client):
    """Раньше длинная новость на главной показывалась текстовым превью
    без разметки (news_format.excerpt — сплошная строка, переносы абзацев
    схлопнуты в пробелы) — «сбитое форматирование» до перехода на полную
    страницу. Теперь на главной всегда полный render_news_html(), как и на
    самой странице новости — просто визуально свёрнутый по высоте (CSS
    .is-collapsible), без обрезки текста и без перехода на другую страницу."""
    long_body = "**Жирный текст.** " + ("Много слов подряд. " * 40)
    db.add(News(title="Длинная новость", body=long_body))
    db.commit()

    body = client.get("/").get_data(as_text=True)
    assert "<strong>Жирный текст.</strong>" in body
    # data-expand-label — атрибут самой кнопки, а не текст комментария в
    # общем CSS (в отличие от голых классов is-collapsible/
    # js-collapsible-toggle, которые упоминаются в комментарии в base.html
    # на КАЖДОЙ странице и потому не годятся как точный маркер).
    assert 'data-expand-label="Читать дальше ↓"' in body
    assert '/news/' not in body  # никакой ссылки на отдельную страницу


def test_home_short_news_has_no_collapse_toggle(db, client):
    db.add(News(title="Короткая новость", body="Всего пара слов."))
    db.commit()

    body = client.get("/").get_data(as_text=True)
    assert "Короткая новость" in body
    assert 'data-expand-label="' not in body


def test_home_shows_surveillance_section_without_recorders(client):
    body = client.get("/").get_data(as_text=True)
    assert "Видеонаблюдение" in body
    assert "Регистраторы видеонаблюдения ещё не добавлены." in body


def test_home_shows_public_bulletin_post_to_anonymous(db, client):
    db.add(BulletinPost(
        category=BulletinCategory.SELL, title="Продам гараж", description="x",
        contact="+7 900 000-00-00", is_members_only=False,
    ))
    db.commit()

    body = client.get("/").get_data(as_text=True)
    assert "Продам гараж" in body


def test_home_hides_members_only_bulletin_post_from_anonymous(db, client):
    db.add(BulletinPost(
        category=BulletinCategory.SELL, title="Только для своих", description="x",
        contact="+7 900 000-00-00", is_members_only=True,
    ))
    db.commit()

    anon_body = client.get("/").get_data(as_text=True)
    assert "Только для своих" not in anon_body

    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    member_body = client.get("/").get_data(as_text=True)
    assert "Только для своих" in member_body


def test_home_still_reachable_directly_for_board(db, client):
    """Дашборд — рабочая "главная" для правления по умолчанию, но сама
    главная сайта (/) при прямом переходе по-прежнему отдаёт 200, не
    редиректит правление куда-либо ещё."""
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/")
    assert resp.status_code == 200


def test_navbar_brand_links_to_dashboard_for_board_and_home_for_member(db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()

    login(client, "board1", "pass12345")
    board_body = client.get("/dashboard").get_data(as_text=True)
    assert 'href="/dashboard"' in board_body
    client.get("/auth/logout")

    login(client, "member1", "pass12345")
    member_body = client.get("/").get_data(as_text=True)
    assert 'href="/dashboard"' not in member_body
    assert 'class="navbar-brand" href="/"' in member_body


def test_login_dropdown_present_only_for_anonymous(db, client):
    anon_body = client.get("/").get_data(as_text=True)
    assert 'name="username"' in anon_body

    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    member_body = client.get("/").get_data(as_text=True)
    assert 'name="username"' not in member_body


def test_login_from_arbitrary_page_returns_there_not_to_dashboard(db, client):
    """Имитирует сабмит дропдауна «Войти» из шапки со страницы доски
    объявлений — next не дашборд/кабинет, а именно та же страница."""
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()

    resp = client.post(
        "/auth/login", data={"username": "board1", "password": "pass12345"},
        query_string={"next": "/bulletin/"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/bulletin/"


def test_login_from_login_page_without_next_lands_on_home_for_member(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()

    resp = client.post("/auth/login", data={"username": "member1", "password": "pass12345"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_login_from_login_page_without_next_lands_on_dashboard_for_board(db, client):
    """Для правления/председателя дашборд остаётся рабочей "главной" — в
    отличие от рядовых членов, их без явного next уводит не на главную
    сайта, а сразу на дашборд (см. auth._complete_login)."""
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()

    resp = client.post("/auth/login", data={"username": "board1", "password": "pass12345"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_login_required_redirect_still_returns_to_originally_requested_page(db, client):
    """Регрессия: login_required-редирект (например, на /wiki/) с явным
    next по-прежнему должен возвращать туда же после входа — это не
    затронуто переносом дефолта на главную (см. auth._complete_login)."""
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()

    resp = client.get("/wiki/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/auth/login?next=")
    assert "wiki" in resp.headers["Location"]

    resp = client.post(
        "/auth/login", data={"username": "member1", "password": "pass12345"},
        query_string={"next": "/wiki/"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/wiki/"
