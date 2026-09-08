"""
Общий CSS в base.html — единый стиль ссылок по всему проекту (без
подчёркивания по умолчанию, подчёркивание только при наведении, см.
`a { text-decoration: none; } a:hover:not(.btn) { ... }`). Кнопки,
свёрстанные тегом <a class="btn ..."> (переход по ссылке, оформленный
как кнопка), а не <button>, исключены из подчёркивания на hover —
Bootstrap сам такого не делает, подчёркивание появлялось только из-за
собственного правила a:hover проекта.
"""
def test_link_hover_underline_excludes_buttons(client, db):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "a { text-decoration: none; }" in body
    assert "a:hover:not(.btn) { text-decoration: underline; }" in body
