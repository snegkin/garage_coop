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
