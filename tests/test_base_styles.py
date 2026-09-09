"""
Общий CSS в base.html — единый стиль ссылок по всему проекту (без
подчёркивания по умолчанию, подчёркивание только при наведении, см.
`a { text-decoration: none; } a:hover:not(.btn):not(.navbar-brand)
:not(.nav-link):not(.dropdown-item) { ... }`). Это касается только
неоформленных ссылок (обычный синий подчёркнутый текст в контенте) — не
элементов меню/навигации: кнопки, свёрстанные тегом <a class="btn ...">
(переход по ссылке, оформленный как кнопка), лого-ссылка с названием
кооператива в шапке (`.navbar-brand`), пункты навбара и дропдаунов
(`.nav-link` — обычные пункты меню, вкладки, кнопка смены темы,
переключатель языка, меню профиля; `.dropdown-item` — пункты внутри них)
исключены из подчёркивания на hover — Bootstrap сам такого не делает,
подчёркивание появлялось только из-за собственного правила a:hover
проекта.
"""
from app.models import RoleEnum

from tests.conftest import make_user, login


def test_link_hover_underline_excludes_buttons_navbar_brand_and_menu_items(client, db):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "a { text-decoration: none; }" in body
    assert (
        "a:hover:not(.btn):not(.navbar-brand):not(.nav-link):not(.dropdown-item) "
        "{ text-decoration: underline; }"
    ) in body


def test_navbar_brand_link_present_when_logged_in(client, db):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="navbar-brand"' in body
