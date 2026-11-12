"""
Галерея «подручных» картинок для новостей (app/news_gallery.py): загрузка,
вставка копией в новость, удаление без вреда для старых новостей.
"""
import io
import os

from app.models import News, NewsAttachment, NewsGalleryImage, RoleEnum

from tests.conftest import make_user, login

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def _login_board(db, client, username="gallery_board"):
    user = make_user(db, username, "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, username, "pass1234")
    return user


def _upload(client, title="Субботник", names=("subbotnik.png",), ajax=False):
    data = {"title": title, "images": [(io.BytesIO(PNG), name) for name in names]}
    headers = {"X-Requested-With": "XMLHttpRequest"} if ajax else {}
    return client.post("/news/gallery/upload", data=data, content_type="multipart/form-data", headers=headers)


def test_upload_and_list(app, db, client):
    _login_board(db, client)
    resp = _upload(client, names=("a.png", "b.png"))
    assert resp.status_code == 302
    images = db.query(NewsGalleryImage).order_by(NewsGalleryImage.id).all()
    assert [i.title for i in images] == ["Субботник", "Субботник"]
    for image in images:
        assert os.path.isfile(os.path.join(app.config["UPLOAD_FOLDER"], image.stored_filename))
    page = client.get("/news/gallery/").get_data(as_text=True)
    assert "Субботник" in page
    assert client.get(f"/news/gallery/{images[0].id}/image").status_code == 200


def test_upload_title_defaults_to_filename_and_rejects_non_images(db, client):
    _login_board(db, client)
    _upload(client, title="", names=("Собрание 2026.png",))
    assert db.query(NewsGalleryImage).one().title == "Собрание 2026"
    resp = _upload(client, names=("evil.svg",), ajax=True)
    assert resp.status_code == 400 and db.query(NewsGalleryImage).count() == 1


def test_use_copies_into_news_and_survives_gallery_delete(app, db, client):
    user = _login_board(db, client)
    _upload(client)
    image = db.query(NewsGalleryImage).one()

    resp = client.post(f"/news/gallery/{image.id}/use", headers={"X-Requested-With": "XMLHttpRequest"})
    url = resp.get_json()["url"]
    att = db.query(NewsAttachment).one()
    assert att.news_id is None and att.is_inline and att.author_id == user.id
    assert att.stored_filename != image.stored_filename
    db.refresh(image)
    assert image.use_count == 1 and image.last_used_at is not None

    client.post("/news/new", data={"title": "Субботник 20 апреля", "body": f"![]({url})\n\nПриходите!"})
    news = db.query(News).one()
    db.refresh(att)
    assert att.news_id == news.id  # новость забрала вложение

    client.post(f"/news/gallery/{image.id}/delete")
    assert db.query(NewsGalleryImage).count() == 0
    assert not os.path.exists(os.path.join(app.config["UPLOAD_FOLDER"], image.stored_filename))
    assert os.path.isfile(os.path.join(app.config["UPLOAD_FOLDER"], att.stored_filename))
    assert client.get(url).status_code == 200


def test_picker_orders_by_use(db, client):
    _login_board(db, client)
    _upload(client, title="Редкая")
    _upload(client, title="Частая")
    rare, frequent = db.query(NewsGalleryImage).order_by(NewsGalleryImage.id).all()
    for _ in range(3):
        client.post(f"/news/gallery/{frequent.id}/use")
    picker = client.get("/news/gallery/picker").get_data(as_text=True)
    assert picker.index("Частая") < picker.index("Редкая")
    assert "<html" not in picker


def test_rename(db, client):
    _login_board(db, client)
    _upload(client, title="Старое")
    image = db.query(NewsGalleryImage).one()
    client.post(f"/news/gallery/{image.id}/rename", data={"title": "Тарифы на электроэнергию"})
    db.refresh(image)
    assert image.title == "Тарифы на электроэнергию"


def test_form_has_gallery_button(db, client):
    _login_board(db, client)
    assert 'id="newsGalleryModal"' in client.get("/news/new").get_data(as_text=True)


def test_members_have_no_access(db, client):
    make_user(db, "gallery_member", "pass1234", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "gallery_member", "pass1234")
    assert client.get("/news/gallery/").status_code in (302, 403)
    assert _upload(client).status_code in (302, 403)
    assert db.query(NewsGalleryImage).count() == 0
