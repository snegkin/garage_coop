"""
Тесты на вики кооператива (app/wiki.py, WikiPage) — справочные заметки,
организованные ДЕРЕВОМ разделов/подразделов (WikiPage.parent_id), не
хронологической лентой, как News. Видимость настраивается ПО СТРАНИЦЕ
через WikiPage.is_internal — тот же принцип, что у Document.is_internal
(см. test_cooperative_documents.py): общедоступная страница видна любому вошедшему
члену, внутренняя — только правлению (не только в списке-дереве, но и при
прямом обращении по id — IDOR). Создают/редактируют/удаляют только
правление независимо от is_internal.

Покрывает также: скрытые узлы не рвут дерево — их видимые потомки
поднимаются к ближайшему видимому предку (_build_visible_tree); нельзя
удалить раздел с детьми; нельзя сделать родителем себя/своего потомка
(защита от цикла).
"""
from app.models import RoleEnum, WikiPage

from tests.conftest import make_person, make_user, login


def _make_page(db, is_internal=False, title="Страница", parent_id=None, body="Текст"):
    page = WikiPage(title=title, parent_id=parent_id, body=body, is_internal=is_internal)
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


# ---------------------------------------------------------------------------
# Видимость (is_internal)
# ---------------------------------------------------------------------------

def test_member_does_not_see_internal_page_in_tree(db, client):
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


# ---------------------------------------------------------------------------
# Дерево: скрытый предок не должен «рвать» видимость потомков
# ---------------------------------------------------------------------------

def test_visible_child_of_internal_section_promoted_to_root_for_member(db, client):
    section = _make_page(db, is_internal=True, title="Internal Section")
    child = _make_page(db, is_internal=False, title="Public Child", parent_id=section.id)
    db.commit()
    _make_member(db)

    login(client, "member1", "pass1234")
    resp = client.get("/wiki/")
    body = resp.get_data(as_text=True)
    assert "Internal Section" not in body
    assert "Public Child" in body, "visible child of a hidden section must still be reachable in the tree"

    resp = client.get(f"/wiki/{child.id}")
    assert resp.status_code == 200


def test_board_sees_full_nesting(db, client):
    section = _make_page(db, is_internal=False, title="Section")
    sub = _make_page(db, is_internal=False, title="Subsection", parent_id=section.id)
    leaf = _make_page(db, is_internal=False, title="Leaf Page", parent_id=sub.id)
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.get("/wiki/")
    body = resp.get_data(as_text=True)
    assert "Section" in body and "Subsection" in body and "Leaf Page" in body


# ---------------------------------------------------------------------------
# Права
# ---------------------------------------------------------------------------

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
        "body": "логин/пароль",
        "is_internal": "on",
    })
    assert resp.status_code == 302
    page = db.query(WikiPage).filter_by(title="Camera creds").first()
    assert page is not None
    assert page.is_internal is True


def test_board_create_page_with_parent(db, client):
    section = _make_page(db, is_internal=False, title="Видеонаблюдение")
    db.commit()
    _make_board(db)
    login(client, "board1", "pass1234")

    resp = client.post("/wiki/new", data={
        "title": "Камера №1", "body": "IP: 10.0.0.5", "parent_id": str(section.id),
    })
    assert resp.status_code == 302
    page = db.query(WikiPage).filter_by(title="Камера №1").first()
    assert page is not None
    assert page.parent_id == section.id


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
    page = _make_page(db, is_internal=False, title="Old Title")
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/edit", data={
        "title": "New Title", "body": "новый текст", "is_internal": "on",
    })
    assert resp.status_code == 302
    updated = db.query(WikiPage).get(page.id)
    assert updated.title == "New Title"
    assert updated.is_internal is True


def test_board_can_delete_leaf_page(db, client):
    page = _make_page(db, is_internal=False, title="To Delete")
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/delete")
    assert resp.status_code == 302
    assert db.query(WikiPage).get(page.id) is None


# ---------------------------------------------------------------------------
# Целостность дерева
# ---------------------------------------------------------------------------

def test_cannot_delete_section_with_children(db, client):
    section = _make_page(db, is_internal=False, title="Section")
    _make_page(db, is_internal=False, title="Child", parent_id=section.id)
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{section.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.query(WikiPage).get(section.id) is not None, "section with children must not be deleted"


def test_cannot_set_self_as_parent(db, client):
    page = _make_page(db, is_internal=False, title="Page")
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/edit", data={
        "title": "Page", "body": "текст", "parent_id": str(page.id),
    })
    assert resp.status_code == 200  # переотрисовка формы с ошибкой, не редирект
    unchanged = db.query(WikiPage).get(page.id)
    assert unchanged.parent_id is None


def test_cannot_set_descendant_as_parent(db, client):
    root = _make_page(db, is_internal=False, title="Root")
    child = _make_page(db, is_internal=False, title="Child", parent_id=root.id)
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    # пытаемся сделать Root ребёнком его же потомка Child — цикл
    resp = client.post(f"/wiki/{root.id}/edit", data={
        "title": "Root", "body": "текст", "parent_id": str(child.id),
    })
    assert resp.status_code == 200
    unchanged = db.query(WikiPage).get(root.id)
    assert unchanged.parent_id is None


def test_reparent_to_valid_new_parent_works(db, client):
    section_a = _make_page(db, is_internal=False, title="Section A")
    section_b = _make_page(db, is_internal=False, title="Section B")
    page = _make_page(db, is_internal=False, title="Movable", parent_id=section_a.id)
    db.commit()
    _make_board(db)

    login(client, "board1", "pass1234")
    resp = client.post(f"/wiki/{page.id}/edit", data={
        "title": "Movable", "body": "текст", "parent_id": str(section_b.id),
    })
    assert resp.status_code == 302
    moved = db.query(WikiPage).get(page.id)
    assert moved.parent_id == section_b.id
