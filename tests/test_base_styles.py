"""
Общий CSS в base.html — единый стиль ссылок по всему проекту (без
подчёркивания по умолчанию, подчёркивание только при наведении, см.
`a { text-decoration: none; } a:hover:not(.btn):not(.navbar-brand) { ...
}`). Кнопки, свёрстанные тегом <a class="btn ..."> (переход по ссылке,
оформленный как кнопка), а не <button>, и лого-ссылка с названием
кооператива в шапке (`.navbar-brand`) исключены из подчёркивания на
hover — Bootstrap сам такого не делает, подчёркивание появлялось только
из-за собственного правила a:hover проекта.
"""
from app.models import RoleEnum

from tests.conftest import make_user, login


def test_link_hover_underline_excludes_buttons_and_navbar_brand(client, db):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "a { text-decoration: none; }" in body
    assert "a:hover:not(.btn):not(.navbar-brand) { text-decoration: underline; }" in body


def test_navbar_brand_link_present_when_logged_in(client, db):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="navbar-brand"' in body
