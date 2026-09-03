"""
Группировка картинок и лайтбокс в тексте новостей/вики (app/templates/base.html:
groupContentImages/initLightbox, .content-image-group/.js-lightbox).

Само перегруппирование абзацев и открытие лайтбокса — клиентский JS, тестами
через app.test_client() не проверяется (в песочнице нет браузера). Здесь
проверяется то, что реально формирует сервер: разметка, на которую JS
опирается — markdown "![](url)" отдельной строкой должен рендериться
единственным <p><img></p>, чтобы JS мог найти и сгруппировать такие абзацы;
общий на всю страницу оверлей #lightboxOverlay должен присутствовать и на
анонимно доступной странице новости, и на странице вики.
"""
from app.models import RoleEnum

from tests.conftest import make_person, make_user, login


def _board_user(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def test_lone_markdown_image_renders_as_single_image_only_paragraph(db, client):
    """Предпосылка для JS-группировки: "![](url)" отдельной строкой markdown
    должен дать <p> с ровно одним ребёнком <img> и без прочего текста."""
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/preview", data={"body": "![](/x.png)"})
    html = resp.get_json()["html"]
    assert html.strip() == '<p><img alt="" src="/x.png"></p>' or (
        "<p><img" in html and html.count("<p>") == 1 and "текст" not in html
    )


def test_news_view_contains_lightbox_overlay_and_js_lightbox_images(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/new", data={
        "title": "Новость с картинками",
        "body": "Текст\n\n![](/a.png)\n\n![](/b.png)\n\nЕщё текст",
    }, follow_redirects=True)
    assert resp.status_code == 200

    from app.models import News
    item = db.query(News).filter_by(title="Новость с картинками").one()

    resp = client.get(f"/news/{item.id}")
    html = resp.get_data(as_text=True)
    assert "lightboxOverlay" in html
    assert html.count('<img alt="" src="/a.png">') == 1
    assert html.count('<img alt="" src="/b.png">') == 1


def test_news_gallery_attachments_use_lightbox_not_new_tab(db, client, app):
    """Галерея вложений (не inline-картинки в тексте) раньше открывалась
    ссылкой target="_blank" — теперь должна попадать в общий лайтбокс так же,
    как inline-картинки."""
    import io

    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/new", data={"title": "N", "body": "Текст"}, follow_redirects=True)
    from app.models import News
    item = db.query(News).filter_by(title="N").one()

    fake_jpeg = b"\xff\xd8\xff\xe0" + b"0" * 50
    client.post(f"/news/{item.id}/edit", data={
        "title": "N", "body": "Текст",
        "attachments": (io.BytesIO(fake_jpeg), "photo.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)

    resp = client.get(f"/news/{item.id}")
    html = resp.get_data(as_text=True)
    assert 'target="_blank"' not in html
    assert "js-lightbox" in html


def test_wiki_view_contains_lightbox_overlay(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    client.post("/wiki/new", data={"title": "Страница", "body": "![](/x.png)"}, follow_redirects=True)
    from app.models import WikiPage
    page = db.query(WikiPage).filter_by(title="Страница").one()

    resp = client.get(f"/wiki/{page.id}")
    html = resp.get_data(as_text=True)
    assert "lightboxOverlay" in html
    assert '<img alt="" src="/x.png">' in html


def test_anonymous_news_page_also_has_lightbox_overlay(db, client):
    """Страница новости доступна анонимно — оверлей должен присутствовать вне
    зависимости от {% if current_user %} в базовом шаблоне (см. навбар)."""
    _board_user(db)
    login(client, "board1", "pass1234")
    client.post("/news/new", data={"title": "Публичная", "body": "![](/x.png)"}, follow_redirects=True)
    from app.models import News
    item = db.query(News).filter_by(title="Публичная").one()
    client.get("/auth/logout")

    resp = client.get(f"/news/{item.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "lightboxOverlay" in html
