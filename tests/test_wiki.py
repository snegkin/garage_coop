"""
Тесты на вики кооператива (app/wiki.py, WikiPage) — справочные заметки
(параметры видеонаблюдения, схема сети, телефоны контрагентов и аварийных
служб). Видимость настраивается ПО СТРАНИЦЕ через WikiPage.is_internal —
тот же принцип, что у Document.is_internal (см. test_documents.py):
общедоступная страница видна любому вошедшему члену, внутренняя — только
правлению (не только в списке, но и при прямом обращении по id — IDOR).
Создают/редактируют/удаляют только правление независимо от is_internal.
"""
from app.models import RoleEnum, WikiPage

from tests.conftest import make_person, make_user, login


def _make_page(db, is_internal, title="Страница", category=None, body="Текст"):
    page = WikiPage(title=title, category=category, body=body, is_internal=is_internal)
    db.add(page)
    db.flush()
    return page


def _make_member(db, username="member1"):
    person = make_person(db, full_name="Member One")
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()


def _make_board(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def test_member_does_not_see_internal_page_in_list(db, client):
    _make_page(db, is_internal=False, title="Public Page")
    _make_page(db, is_internal=True, title="Internal Page")
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get("/wiki/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Public Page" in body
    assert "Internal Page" not in body


def test_board_sees_both_public_and_internal_pages(db, client):
    _make_page(db, is_internal=False, title="Public Page")
    _make_page(db, is_internal=True, title="Internal Page")
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.get("/wiki/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Public Page" in body
    assert "Internal Page" in body


def test_member_cannot_open_internal_page_directly(db, client):
    """IDOR-проверка: даже зная id и прямой URL /wiki/<id>, рядовой член
    не должен получить содержимое внутренней страницы."""
    page = _make_page(db, is_internal=True, title="Internal Page")
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get(f"/wiki/{page.id}")
    assert resp.status_code == 403


def test_member_can_open_public_page(db, client):
    page = _make_page(db, is_internal=False, title="Public Page")
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get(f"/wiki/{page.id}")
    assert resp.status_code == 200


def test_only_board_can_create_page(client, db):
    _make_member(db)
    login(client, "member1", "pass1234")
    resp = client.post("/wiki/new", data={
        "title": "Sneaky page",
        "body": "текст",
    })
    assert resp.status_code == 302
    assert db.query(WikiPage).filter_by(title="Sneaky page").first() is None


def test_board_create_page_checkbox_sets_is_internal(db, client):
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.post("/wiki/new", data={
        "title": "Camera creds",
        "category": "видеонаблюдение",
        "body": "логин/пароль",
        "is_internal": "on",
    })
    assert resp.status_code == 302
    page = db.query(WikiPage).filter_by(title="Camera creds").first()
    assert page is not None
    assert page.is_internal is True
    assert page.category == "видеонаблюдение"


def test_board_create_page_without_checkbox_is_public(db, client):
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.post("/wiki/new", data={
        "title": "Emergency phones",
        "body": "112",
    })
    assert resp.status_code == 302
    page = db.query(WikiPage).filter_by(title="Emergency phones").first()
    assert page is not None
    assert page.is_internal is False


def test_member_cannot_edit_or_delete_page(db, client):
    page = _make_page(db, is_internal=False, title="Public Page")
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/edit", data={"title": "Hacked", "body": "x"})
    assert resp.status_code == 302
    assert db.query(WikiPage).get(page.id).title == "Public Page"

    resp = client.post(f"/wiki/{page.id}/delete")
    assert resp.status_code == 302
    assert db.query(WikiPage).get(page.id) is not None


def test_board_can_edit_page(db, client):
    page = _make_page(db, is_internal=False, title="Old Title", category=None)
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/edit", data={
        "title": "New Title",
        "category": "сеть",
        "body": "новый текст",
        "is_internal": "on",
    })
    assert resp.status_code == 302
    updated = db.query(WikiPage).get(page.id)
    assert updated.title == "New Title"
    assert updated.category == "сеть"
    assert updated.is_internal is True


def test_board_can_delete_page(db, client):
    page = _make_page(db, is_internal=False, title="To Delete")
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/delete")
    assert resp.status_code == 302
    assert db.query(WikiPage).get(page.id) is None


def test_category_filter(db, client):
    _make_page(db, is_internal=False, title="Cam Page", category="видеонаблюдение")
    _make_page(db, is_internal=False, title="Net Page", category="сеть")
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get("/wiki/?category=видеонаблюдение")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Cam Page" in body
    assert "Net Page" not in body
