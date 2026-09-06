"""
Чат правления (app/board_chat.py, /board-chat/) — общий групповой чат для
BOARD/ACCOUNTANT/CHAIRMAN, плавающий виджет на всех страницах (см.
base.html). GET /messages одновременно и есть "прочтение" — обновляет
User.board_chat_read_at, используемый для бейджа непрочитанных
(app/__init__.py: _inject_user -> board_chat_unread_count).
"""
import datetime as dt

from app.models import RoleEnum, BoardChatMessage, User

from tests.conftest import make_person, make_user, login


def _board_user(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


# ---------------------------------------------------------------------------
# Права
# ---------------------------------------------------------------------------

def test_anonymous_cannot_list_messages(client, db):
    resp = client.get("/board-chat/messages")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_plain_member_cannot_list_messages(db, client):
    person = make_person(db, full_name="Member One")
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member1", "pass1234")

    resp = client.get("/board-chat/messages")
    assert resp.status_code == 302  # roles_required редиректит, не 403


def test_plain_member_cannot_send_message(db, client):
    person = make_person(db, full_name="Member Two")
    make_user(db, "member2", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member2", "pass1234")

    resp = client.post("/board-chat/messages", data={"body": "тест"})
    assert resp.status_code == 302
    assert db.query(BoardChatMessage).count() == 0


def test_board_member_can_list_and_send(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.get("/board-chat/messages")
    assert resp.status_code == 200

    resp = client.post("/board-chat/messages", data={"body": "привет"})
    assert resp.status_code == 200
    assert db.query(BoardChatMessage).count() == 1


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def test_send_message_records_author(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/board-chat/messages", data={"body": "сообщение"})
    data = resp.get_json()
    assert data["message"]["body"] == "сообщение"
    assert data["message"]["is_mine"] is True

    user = db.query(User).filter_by(username="board1").one()
    message = db.query(BoardChatMessage).one()
    assert message.author_id == user.id
    assert message.body == "сообщение"


def test_send_empty_message_is_rejected(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/board-chat/messages", data={"body": "   "})
    assert resp.status_code == 400
    assert db.query(BoardChatMessage).count() == 0


def test_author_name_prefers_full_name_over_username(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")
    resp = client.post("/board-chat/messages", data={"body": "привет"})
    assert resp.get_json()["message"]["author_name"] == "Board One"


def test_author_name_falls_back_to_username_without_person(db, client):
    make_user(db, "noPerson1", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "noPerson1", "pass1234")

    resp = client.post("/board-chat/messages", data={"body": "привет"})
    assert resp.get_json()["message"]["author_name"] == "noPerson1"


# ---------------------------------------------------------------------------
# Список сообщений / after_id
# ---------------------------------------------------------------------------

def test_list_messages_returns_recent_in_chronological_order(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    for text in ("первое", "второе", "третье"):
        client.post("/board-chat/messages", data={"body": text})

    resp = client.get("/board-chat/messages")
    bodies = [m["body"] for m in resp.get_json()["messages"]]
    assert bodies == ["первое", "второе", "третье"]


def test_list_messages_after_id_returns_only_newer(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")

    first = client.post("/board-chat/messages", data={"body": "первое"}).get_json()["message"]
    client.post("/board-chat/messages", data={"body": "второе"})

    resp = client.get(f"/board-chat/messages?after_id={first['id']}")
    bodies = [m["body"] for m in resp.get_json()["messages"]]
    assert bodies == ["второе"]


def test_other_users_message_is_not_mine(db, client):
    _board_user(db, "board1")
    _board_user(db, "board2")
    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "от board1"})
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    resp = client.get("/board-chat/messages")
    messages = resp.get_json()["messages"]
    assert len(messages) == 1
    assert messages[0]["is_mine"] is False


# ---------------------------------------------------------------------------
# Бейдж непрочитанных (board_chat_unread_count, app/__init__.py)
# ---------------------------------------------------------------------------

def test_unread_count_increases_for_other_users_message(db, client):
    _board_user(db, "board1")
    _board_user(db, "board2")
    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "от board1"})
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True) if resp.status_code == 200 else client.get("/cabinet/garages").get_data(as_text=True)
    assert '<span id="boardChatBadge" class="chat-widget-badge ">1</span>' in body or ">1</span>" in body


def test_polling_messages_marks_as_read_and_clears_badge(db, client):
    _board_user(db, "board1")
    _board_user(db, "board2")
    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "от board1"})
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    client.get("/board-chat/messages")  # опрос == прочтение

    user2 = db.query(User).filter_by(username="board2").one()
    assert user2.board_chat_read_at is not None

    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True) if resp.status_code == 200 else client.get("/cabinet/garages").get_data(as_text=True)
    assert 'class="chat-widget-badge d-none"' in body


def test_own_messages_are_never_counted_as_unread(db, client):
    _board_user(db, "board1")
    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "своё сообщение"})

    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True) if resp.status_code == 200 else client.get("/cabinet/garages").get_data(as_text=True)
    assert 'class="chat-widget-badge d-none"' in body


def test_new_message_after_read_increases_badge_again(db, client):
    _board_user(db, "board1")
    _board_user(db, "board2")
    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "первое"})
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    client.get("/board-chat/messages")
    client.get("/auth/logout")

    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "второе"})
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True) if resp.status_code == 200 else client.get("/cabinet/garages").get_data(as_text=True)
    assert ">1</span>" in body


# ---------------------------------------------------------------------------
# Виджет на странице
# ---------------------------------------------------------------------------

def test_widget_present_for_board_member(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")
    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True)
    assert 'id="boardChatWidget"' in body


def test_widget_absent_for_plain_member(db, client):
    person = make_person(db, full_name="Member Three")
    make_user(db, "member3", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member3", "pass1234")

    resp = client.get("/cabinet/garages")
    body = resp.get_data(as_text=True)
    assert 'id="boardChatWidget"' not in body


def test_widget_absent_for_anonymous(client):
    resp = client.get("/auth/login")
    body = resp.get_data(as_text=True)
    assert 'id="boardChatWidget"' not in body
