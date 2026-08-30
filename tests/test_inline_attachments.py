"""
Тесты на inline-картинки в теле новости и страницы вики (app/news.py,
app/wiki.py: upload_inline_attachment/_sync_inline_attachments,
app/models.py: NewsAttachment.is_inline/WikiAttachment).

Покрывает: загрузка создаёт «осиротевшее» вложение (news_id/page_id=NULL);
только правление может грузить; сохранение статьи «забирает» осиротевшее
вложение, упомянутое в тексте; убрать ссылку из текста при правке — вложение
удаляется (и файл с диска — event-listener в models.py); чужие/уже занятые
вложения не подхватываются; видимость файла вики наследует is_internal
страницы, включая случай ещё не сохранённой (осиротевшей) картинки.
"""
import io
import os

from app.models import RoleEnum, News, NewsAttachment, WikiPage, WikiAttachment

from tests.conftest import make_person, make_user, login

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 50


def _board_user(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def _member_user(db, username="member1"):
    person = make_person(db, full_name="Member One")
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()


def _upload(client, endpoint, filename="photo.jpg"):
    return client.post(endpoint, data={"image": (io.BytesIO(FAKE_JPEG), filename)}, content_type="multipart/form-data")


# ---------------------------------------------------------------------------
# Новости
# ---------------------------------------------------------------------------

def test_upload_creates_orphan_inline_attachment(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = _upload(client, "/news/attachments/upload")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "url" in data

    att = db.query(NewsAttachment).filter_by(is_inline=True).first()
    assert att is not None
    assert att.news_id is None


def test_only_board_can_upload_inline_image(db, client):
    _member_user(db)
    login(client, "member1", "pass1234")

    resp = _upload(client, "/news/attachments/upload")
    assert resp.status_code in (302, 403)
    assert db.query(NewsAttachment).count() == 0


def test_create_news_claims_referenced_orphan_image(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = _upload(client, "/news/attachments/upload")
    url = resp.get_json()["url"]

    resp = client.post("/news/new", data={
        "title": "С картинкой",
        "body": f"Текст\n\n![]({url})",
    }, follow_redirects=True)
    assert resp.status_code == 200

    item = db.query(News).filter_by(title="С картинкой").first()
    assert item is not None
    assert len(item.attachments) == 1
    assert item.attachments[0].is_inline is True
    assert item.attachments[0].news_id == item.id


def test_removing_image_reference_on_edit_deletes_attachment_and_file(db, client, app):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = _upload(client, "/news/attachments/upload")
    url = resp.get_json()["url"]
    att_id = int(url.split("/")[3])

    stored_path = os.path.join(app.config["UPLOAD_FOLDER"], db.get(NewsAttachment, att_id).stored_filename)
    assert os.path.exists(stored_path)

    client.post("/news/new", data={"title": "N", "body": f"![]({url})"}, follow_redirects=True)
    item = db.query(News).filter_by(title="N").first()
    assert len(item.attachments) == 1

    client.post(f"/news/{item.id}/edit", data={"title": "N", "body": "Текст без картинки"}, follow_redirects=True)

    assert db.query(NewsAttachment).filter_by(id=att_id).first() is None
    assert not os.path.exists(stored_path)


def test_gallery_attachment_survives_edit_regardless_of_body_text(db, client):
    """Классические (не inline) вложения из блока «Добавить фото или файлы»
    не должны затрагиваться синхронизацией inline-вложений — управляются
    только чекбоксами remove_attachment."""
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/news/new", data={
        "title": "G",
        "body": "Просто текст, без markdown-картинок",
        "attachments": (io.BytesIO(FAKE_JPEG), "gallery.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert resp.status_code == 200

    item = db.query(News).filter_by(title="G").first()
    assert len(item.attachments) == 1
    assert item.attachments[0].is_inline is False

    client.post(f"/news/{item.id}/edit", data={"title": "G", "body": "Текст изменился"}, follow_redirects=True)
    item = db.query(News).get(item.id)
    assert len(item.attachments) == 1, "gallery attachment should not be pruned by inline-sync"


def test_cannot_claim_another_authors_orphan(db, client):
    """Осиротевшее вложение другого автора не подхватывается по одной лишь
    ссылке в тексте — иначе можно было бы «угнать» чужую загрузку."""
    _board_user(db, "board1")
    _board_user(db, "board2")

    login(client, "board1", "pass1234")
    resp = _upload(client, "/news/attachments/upload")
    url = resp.get_json()["url"]
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    client.post("/news/new", data={"title": "Hijack", "body": f"![]({url})"}, follow_redirects=True)

    item = db.query(News).filter_by(title="Hijack").first()
    assert item is not None
    assert len(item.attachments) == 0, "attachment authored by another user must not be claimed"


# ---------------------------------------------------------------------------
# Вики
# ---------------------------------------------------------------------------

def test_wiki_upload_and_claim(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = _upload(client, "/wiki/attachments/upload")
    assert resp.status_code == 200
    url = resp.get_json()["url"]

    att = db.query(WikiAttachment).first()
    assert att is not None
    assert att.page_id is None

    client.post("/wiki/new", data={
        "title": "Сеть", "body": f"Схема\n\n![]({url})",
    }, follow_redirects=True)

    page = db.query(WikiPage).filter_by(title="Сеть").first()
    assert page is not None
    assert len(page.attachments) == 1
    assert page.attachments[0].page_id == page.id


def test_wiki_internal_page_image_hidden_from_member(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = _upload(client, "/wiki/attachments/upload")
    url = resp.get_json()["url"]
    att_id = int(url.split("/")[3])

    client.post("/wiki/new", data={
        "title": "Камеры", "body": f"![]({url})", "is_internal": "on",
    }, follow_redirects=True)
    client.get("/auth/logout")

    _member_user(db)
    login(client, "member1", "pass1234")
    resp = client.get(f"/wiki/attachments/{att_id}/photo.jpg")
    assert resp.status_code == 403


def test_wiki_orphan_image_not_accessible_to_member(db, client):
    """Ещё не сохранённая (осиротевшая) картинка по умолчанию считается
    внутренней — рядовой член не должен получить её по прямой ссылке."""
    _board_user(db)
    login(client, "board1", "pass1234")
    resp = _upload(client, "/wiki/attachments/upload")
    url = resp.get_json()["url"]
    att_id = int(url.split("/")[3])
    client.get("/auth/logout")

    _member_user(db)
    login(client, "member1", "pass1234")
    resp = client.get(f"/wiki/attachments/{att_id}/photo.jpg")
    assert resp.status_code == 403


def test_wiki_public_page_image_visible_to_member(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")
    resp = _upload(client, "/wiki/attachments/upload")
    url = resp.get_json()["url"]
    att_id = int(url.split("/")[3])

    client.post("/wiki/new", data={"title": "Телефоны", "body": f"![]({url})"}, follow_redirects=True)
    client.get("/auth/logout")

    _member_user(db)
    login(client, "member1", "pass1234")
    resp = client.get(f"/wiki/attachments/{att_id}/photo.jpg")
    assert resp.status_code == 200
