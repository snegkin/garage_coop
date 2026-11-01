"""
Навигация на телефоне (base.html): меню — выдвижная панель (offcanvas #nav)
вместо раскрывающегося блока, нижняя панель вкладок (.bottom-nav) и
дропдаун «Войти», который не закрывается от тапа внутри формы.
"""
import re

from app.models import RoleEnum

from tests.conftest import make_user, login


def _bottom_nav(body):
    start = body.index('<nav class="bottom-nav')
    return body[start:body.index("</nav>", start)]


def test_menu_is_offcanvas_opened_by_burger_and_bottom_menu_button(client, db):
    body = client.get("/").get_data(as_text=True)
    assert 'class="offcanvas offcanvas-end" tabindex="-1" id="nav"' in body
    assert "navbar-collapse" not in body
    # бургер в шапке и «Меню» в нижней панели открывают одну и ту же панель
    assert body.count('data-bs-toggle="offcanvas" data-bs-target="#nav"') == 2
    assert 'data-bs-toggle="offcanvas" data-bs-target="#nav"' in _bottom_nav(body)


def test_login_dropdown_closes_only_on_outside_click(client, db):
    body = client.get("/").get_data(as_text=True)
    assert re.search(r'data-bs-toggle="dropdown" data-bs-auto-close="outside"[^>]*>Войти<', body)


def test_bottom_nav_for_anonymous(client, db):
    nav = _bottom_nav(client.get("/").get_data(as_text=True))
    assert 'class="bottom-nav-item active" href="/" aria-current="page"' in nav
    for href in ("/bulletin", "/auth/login"):
        assert f'href="{href}' in nav
    assert "Профиль" not in nav


def test_bottom_nav_for_member(client, db):
    make_user(db, "member_bn", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member_bn", "pass12345")

    nav = _bottom_nav(client.get("/cabinet/garages").get_data(as_text=True))
    assert re.search(r'class="bottom-nav-item active" href="/cabinet/garages"', nav)
    assert "Квитанции" in nav and "Профиль" in nav
    assert "/auth/login" not in nav


def test_bottom_nav_home_is_dashboard_for_board(client, db):
    make_user(db, "board_bn", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_bn", "pass12345")

    nav = _bottom_nav(client.get("/dashboard").get_data(as_text=True))
    assert re.search(r'class="bottom-nav-item active" href="/dashboard"', nav)
