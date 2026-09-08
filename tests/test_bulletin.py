"""
Доска объявлений (`/bulletin/`, app/bulletin.py) — общедоступная, как
новостная лента (видна и анонимным посетителям), но разместить объявление
может любой ВОШЕДШИЙ пользователь, не только правление. Удалить своё
объявление может сам автор, любое — правление (модерация постфактум, без
предварительного одобрения перед публикацией).
"""
from decimal import Decimal

from app.models import RoleEnum, BulletinPost, BulletinCategory

from tests.conftest import make_user, login


def _post_ad(client, category="sell", title="Продам гараж", description="Хороший гараж", contact="+7 900 000-00-00", price=""):
    return client.post("/bulletin/new", data={
        "category": category, "title": title, "description": description, "contact": contact, "price": price,
    })


# ---------------------------------------------------------------------------
# Видимость — общедоступна
# ---------------------------------------------------------------------------

def test_anonymous_can_view_board(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client, title="Видно всем")
    client.get("/auth/logout")

    resp = client.get("/bulletin/")
    assert resp.status_code == 200
    assert "Видно всем" in resp.get_data(as_text=True)


def test_login_page_has_bulletin_button(client):
    resp = client.get("/auth/login")
    body = resp.get_data(as_text=True)
    assert 'href="/bulletin/"' in body


def test_category_filter(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client, category="buy", title="Куплю велосипед")
    _post_ad(client, category="sell", title="Продам велосипед")

    resp = client.get("/bulletin/?category=buy")
    body = resp.get_data(as_text=True)
    assert "Куплю велосипед" in body
    assert "Продам велосипед" not in body


def test_lease_and_seeking_categories(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client, category="lease", title="Аренда бокса на зиму")
    _post_ad(client, category="seeking", title="Ищу мастера по сигнализациям")

    lease_post = db.query(BulletinPost).filter_by(title="Аренда бокса на зиму").one()
    seeking_post = db.query(BulletinPost).filter_by(title="Ищу мастера по сигнализациям").one()
    assert lease_post.category == BulletinCategory.LEASE
    assert seeking_post.category == BulletinCategory.SEEKING

    resp = client.get("/bulletin/?category=lease")
    body = resp.get_data(as_text=True)
    assert "Аренда бокса на зиму" in body
    assert "Ищу мастера по сигнализациям" not in body


# ---------------------------------------------------------------------------
# Публикация — любой вошедший, без привязки к роли/карточке Person
# ---------------------------------------------------------------------------

def test_anonymous_cannot_post(client, db):
    resp = _post_ad(client)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    assert db.query(BulletinPost).count() == 0


def test_plain_member_can_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = _post_ad(client, category="rent", title="Сдам место", price="500.50")
    assert resp.status_code == 302
    post = db.query(BulletinPost).one()
    assert post.category == BulletinCategory.RENT
    assert post.title == "Сдам место"
    assert post.price == Decimal("500.50")


def test_post_without_required_field_is_rejected(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post("/bulletin/new", data={"category": "sell", "title": "", "description": "x", "contact": "x"})
    assert resp.status_code == 302
    assert db.query(BulletinPost).count() == 0


def test_invalid_price_is_treated_as_not_specified(db, client):
    """Как и везде в проекте у необязательных денежных полей
    (i18n.parse_optional_decimal) — некорректная цена не блокирует
    публикацию, просто трактуется как "не указана"."""
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post("/bulletin/new", data={
        "category": "sell", "title": "x", "description": "x", "contact": "x", "price": "not-a-number",
    })
    assert resp.status_code == 302
    post = db.query(BulletinPost).one()
    assert post.price is None


def test_price_is_optional(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    _post_ad(client, price="")
    post = db.query(BulletinPost).one()
    assert post.price is None


# ---------------------------------------------------------------------------
# Редактирование/удаление — автор своего, правление — любого
# ---------------------------------------------------------------------------

def test_author_can_edit_own_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client, title="Старый заголовок")
    post = db.query(BulletinPost).one()

    resp = client.post(f"/bulletin/{post.id}/edit", data={
        "category": "sell", "title": "Новый заголовок", "description": "x", "contact": "x", "price": "",
    })
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(BulletinPost).one().title == "Новый заголовок"


def test_other_member_cannot_edit_or_delete_someone_elses_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client)
    post = db.query(BulletinPost).one()
    client.get("/auth/logout")

    login(client, "member2", "pass12345")
    resp_edit = client.get(f"/bulletin/{post.id}/edit")
    assert resp_edit.status_code == 403
    resp_delete = client.post(f"/bulletin/{post.id}/delete")
    assert resp_delete.status_code == 403
    assert db.query(BulletinPost).count() == 1


def test_board_can_delete_any_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client)
    post = db.query(BulletinPost).one()
    client.get("/auth/logout")

    login(client, "board1", "pass12345")
    resp = client.post(f"/bulletin/{post.id}/delete")
    assert resp.status_code == 302
    assert db.query(BulletinPost).count() == 0


def test_author_can_delete_own_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client)
    post = db.query(BulletinPost).one()

    resp = client.post(f"/bulletin/{post.id}/delete")
    assert resp.status_code == 302
    assert db.query(BulletinPost).count() == 0


# ---------------------------------------------------------------------------
# «Только для членов кооператива»
# ---------------------------------------------------------------------------

def test_members_only_post_hidden_from_anonymous(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    client.post("/bulletin/new", data={
        "category": "sell", "title": "Секретное объявление", "description": "x", "contact": "x", "price": "",
        "is_members_only": "on",
    })
    post = db.query(BulletinPost).one()
    assert post.is_members_only is True
    client.get("/auth/logout")

    resp = client.get("/bulletin/")
    assert "Секретное объявление" not in resp.get_data(as_text=True)


def test_members_only_post_visible_to_any_logged_in_user(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    client.post("/bulletin/new", data={
        "category": "sell", "title": "Секретное объявление", "description": "x", "contact": "x", "price": "",
        "is_members_only": "on",
    })
    client.get("/auth/logout")

    login(client, "member2", "pass12345")
    resp = client.get("/bulletin/")
    assert "Секретное объявление" in resp.get_data(as_text=True)


def test_public_post_by_default(db, client):
    """Чекбокс не отмечен — объявление, как и раньше, общедоступно."""
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client)
    assert db.query(BulletinPost).one().is_members_only is False


# ---------------------------------------------------------------------------
# Форматирование текста (markdown, как у новостей/вики) и вложения
# ---------------------------------------------------------------------------

def test_description_renders_markdown(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _post_ad(client, description="**жирный текст**")

    resp = client.get("/bulletin/")
    assert "<strong>жирный текст</strong>" in resp.get_data(as_text=True)


def test_preview_endpoint_renders_current_markdown(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post("/bulletin/preview", data={"description": "*курсив*"})
    assert resp.status_code == 200
    assert "<em>курсив</em>" in resp.get_json()["html"]


def test_preview_requires_login(client, db):
    resp = client.post("/bulletin/preview", data={"description": "x"})
    assert resp.status_code == 302


def test_upload_inline_attachment_requires_login(client, db):
    resp = client.post("/bulletin/attachments/upload", data={})
    assert resp.status_code == 302


def test_upload_inline_attachment_creates_orphan_attachment(db, client):
    from io import BytesIO
    from app.models import BulletinAttachment

    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post("/bulletin/attachments/upload", data={
        "image": (BytesIO(b"\x89PNG\r\n\x1a\n"), "pic.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    att = db.query(BulletinAttachment).one()
    assert att.post_id is None
    assert att.is_inline is True
    assert resp.get_json()["url"].startswith("/bulletin/attachments/")


def test_inline_attachment_gets_attached_on_save_when_referenced(db, client):
    from io import BytesIO
    from app.models import BulletinAttachment

    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    upload_resp = client.post("/bulletin/attachments/upload", data={
        "image": (BytesIO(b"\x89PNG\r\n\x1a\n"), "pic.png"),
    }, content_type="multipart/form-data")
    url = upload_resp.get_json()["url"]

    client.post("/bulletin/new", data={
        "category": "sell", "title": "С картинкой", "description": f"![]({url})", "contact": "x", "price": "",
    })
    db.expire_all()
    att = db.query(BulletinAttachment).one()
    assert att.post_id is not None


# ---------------------------------------------------------------------------
# Видимость вложений «только для членов»
# ---------------------------------------------------------------------------

def test_attachment_of_members_only_post_blocked_for_anonymous(db, client):
    from io import BytesIO

    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    upload_resp = client.post("/bulletin/attachments/upload", data={
        "image": (BytesIO(b"\x89PNG\r\n\x1a\n"), "pic.png"),
    }, content_type="multipart/form-data")
    url = upload_resp.get_json()["url"]
    client.post("/bulletin/new", data={
        "category": "sell", "title": "Секрет", "description": f"![]({url})", "contact": "x", "price": "",
        "is_members_only": "on",
    })
    post = db.query(BulletinPost).one()
    att_id = post.attachments[0].id
    client.get("/auth/logout")

    resp = client.get(f"/bulletin/attachments/{att_id}/pic.png")
    assert resp.status_code == 403
