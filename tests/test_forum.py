"""
Форум (`/forum/`, app/forum.py) — свободное обсуждение любых вопросов
вошедшими членами кооператива. В отличие от доски объявлений форум НЕ
общедоступен — все роуты под @login_required. Без разделов/категорий,
один общий список тем. Удалить свою тему/сообщение может автор, любую —
правление (модерация постфактум); закрывать тему для новых ответов может
только правление; редактировать текст — только сам автор (правление
редактировать чужое не может, только удалять).
"""
from io import BytesIO

from app.models import RoleEnum, ForumTopic, ForumPost, ForumAttachment

from tests.conftest import make_user, login


def _create_topic(client, title="Тестовая тема", body="Текст первого сообщения"):
    return client.post("/forum/new", data={"title": title, "body": body})


# ---------------------------------------------------------------------------
# Видимость — только вошедшим
# ---------------------------------------------------------------------------

def test_anonymous_redirected_to_login(client):
    resp = client.get("/forum/")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_member_can_view_list_and_topic(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    _create_topic(client, title="Общий вопрос")
    topic = db.query(ForumTopic).one()

    list_body = client.get("/forum/").get_data(as_text=True)
    assert "Общий вопрос" in list_body

    view_body = client.get(f"/forum/{topic.id}").get_data(as_text=True)
    assert "Общий вопрос" in view_body
    assert "Текст первого сообщения" in view_body


# ---------------------------------------------------------------------------
# Создание темы / ответ
# ---------------------------------------------------------------------------

def test_any_member_can_create_topic_and_reply(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = _create_topic(client)
    assert resp.status_code == 302
    topic = db.query(ForumTopic).one()
    assert topic.author.username == "member1"
    assert db.query(ForumPost).filter_by(topic_id=topic.id).count() == 1

    resp = client.post(f"/forum/{topic.id}/reply", data={"body": "Ответ"})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumPost).filter_by(topic_id=topic.id).count() == 2


def test_create_requires_title_and_body(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post("/forum/new", data={"title": "", "body": ""})
    assert resp.status_code == 302
    assert db.query(ForumTopic).count() == 0


def test_reply_updates_last_activity(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    created_activity = topic.last_activity_at

    client.post(f"/forum/{topic.id}/reply", data={"body": "Ответ"})
    db.expire_all()
    topic = db.query(ForumTopic).one()
    assert topic.last_activity_at >= created_activity


# ---------------------------------------------------------------------------
# Правка — только свой пост
# ---------------------------------------------------------------------------

def test_author_can_edit_own_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client, title="Старый заголовок", body="Старый текст")
    post = db.query(ForumPost).one()

    resp = client.post(f"/forum/posts/{post.id}/edit", data={"title": "Новый заголовок", "body": "Новый текст"})
    assert resp.status_code == 302
    db.expire_all()
    post = db.query(ForumPost).one()
    assert post.body == "Новый текст"
    assert post.topic.title == "Новый заголовок"


def test_other_member_cannot_edit_foreign_post(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    post = db.query(ForumPost).one()
    client.get("/auth/logout")

    login(client, "member2", "pass12345")
    resp = client.post(f"/forum/posts/{post.id}/edit", data={"body": "Чужая правка"})
    assert resp.status_code == 403


def test_board_cannot_edit_foreign_post(db, client):
    """Правление модерирует удалением, не переписыванием чужого текста."""
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    post = db.query(ForumPost).one()
    client.get("/auth/logout")

    login(client, "board1", "pass12345")
    resp = client.post(f"/forum/posts/{post.id}/edit", data={"body": "Правление переписало"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Удаление — автор или правление
# ---------------------------------------------------------------------------

def test_author_can_delete_own_reply(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.post(f"/forum/{topic.id}/reply", data={"body": "Ответ на удаление"})
    reply_post = db.query(ForumPost).filter(ForumPost.body == "Ответ на удаление").one()

    resp = client.post(f"/forum/posts/{reply_post.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumPost).filter_by(id=reply_post.id).first() is None


def test_other_member_cannot_delete_foreign_reply(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.post(f"/forum/{topic.id}/reply", data={"body": "Ответ"})
    reply_post = db.query(ForumPost).filter(ForumPost.body == "Ответ").one()
    client.get("/auth/logout")

    login(client, "member2", "pass12345")
    resp = client.post(f"/forum/posts/{reply_post.id}/delete")
    assert resp.status_code == 403


def test_board_can_delete_foreign_reply(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.post(f"/forum/{topic.id}/reply", data={"body": "Ответ"})
    reply_post = db.query(ForumPost).filter(ForumPost.body == "Ответ").one()
    client.get("/auth/logout")

    login(client, "board1", "pass12345")
    resp = client.post(f"/forum/posts/{reply_post.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumPost).filter_by(id=reply_post.id).first() is None


def test_first_post_cannot_be_deleted_individually(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    first_post = db.query(ForumPost).one()

    resp = client.post(f"/forum/posts/{first_post.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumPost).filter_by(id=first_post.id).first() is not None


def test_author_can_delete_own_topic_cascades_posts(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.post(f"/forum/{topic.id}/reply", data={"body": "Ответ"})

    resp = client.post(f"/forum/{topic.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumTopic).count() == 0
    assert db.query(ForumPost).count() == 0


def test_other_member_cannot_delete_foreign_topic(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "member2", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.get("/auth/logout")

    login(client, "member2", "pass12345")
    resp = client.post(f"/forum/{topic.id}/delete")
    assert resp.status_code == 403


def test_board_can_delete_foreign_topic(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.get("/auth/logout")

    login(client, "board1", "pass12345")
    resp = client.post(f"/forum/{topic.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumTopic).count() == 0


# ---------------------------------------------------------------------------
# Закрытие темы — только правление
# ---------------------------------------------------------------------------

def test_plain_member_cannot_close_topic(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()

    resp = client.post(f"/forum/{topic.id}/close")
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumTopic).one().is_closed is False


def test_board_can_close_and_reopen_topic(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.get("/auth/logout")

    login(client, "board1", "pass12345")
    client.post(f"/forum/{topic.id}/close")
    db.expire_all()
    assert db.query(ForumTopic).one().is_closed is True

    client.post(f"/forum/{topic.id}/reopen")
    db.expire_all()
    assert db.query(ForumTopic).one().is_closed is False


def test_closed_topic_rejects_new_replies(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "member1", "pass12345")
    _create_topic(client)
    topic = db.query(ForumTopic).one()
    client.get("/auth/logout")

    login(client, "board1", "pass12345")
    client.post(f"/forum/{topic.id}/close")
    client.get("/auth/logout")

    login(client, "member1", "pass12345")
    resp = client.post(f"/forum/{topic.id}/reply", data={"body": "Слишком поздно"})
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(ForumPost).filter_by(topic_id=topic.id).count() == 1


# ---------------------------------------------------------------------------
# AJAX-вставка картинки в текст
# ---------------------------------------------------------------------------

def test_upload_inline_attachment_requires_login(client, db):
    resp = client.post("/forum/attachments/upload", data={})
    assert resp.status_code == 302


def test_inline_attachment_gets_attached_on_save_when_referenced(db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    upload_resp = client.post("/forum/attachments/upload", data={
        "image": (BytesIO(b"\x89PNG\r\n\x1a\n"), "pic.png"),
    }, content_type="multipart/form-data")
    assert upload_resp.status_code == 200
    url = upload_resp.get_json()["url"]

    _create_topic(client, title="С картинкой", body=f"![]({url})")
    db.expire_all()
    att = db.query(ForumAttachment).one()
    assert att.post_id is not None
