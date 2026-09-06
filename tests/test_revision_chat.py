"""
Чат ревизионной комиссии (app/revision_chat.py, /revision-chat/) — та же
механика, что и у чата правления (app/board_chat.py), но доступ по
членству в ТЕКУЩЕЙ (не закрытой) ревизионной комиссии
(RevisionCommission/RevisionCommissionMember), а не по User.role — по
уставу комиссия обычно не пересекается с правлением.
"""
import datetime as dt

from app.models import RoleEnum, RevisionChatMessage, RevisionCommission, RevisionCommissionMember, User

from tests.conftest import make_person, make_user, login


def _commission_member(db, username, full_name="Ревизор Ревизорович", is_chair=False, closed=False):
    """Заводит человека — члена ТЕКУЩЕЙ ревизионной комиссии — с учётной
    записью под обычной ролью MEMBER: доступ к чату не завязан на роль."""
    person = make_person(db, full_name=full_name)
    commission = (
        db.query(RevisionCommission).filter_by(end_date=None).first()
        if not closed else None
    )
    if commission is None and not closed:
        commission = RevisionCommission(start_date=dt.date(2026, 1, 1))
        db.add(commission)
        db.flush()
    if closed:
        commission = RevisionCommission(start_date=dt.date(2020, 1, 1), end_date=dt.date(2021, 1, 1))
        db.add(commission)
        db.flush()
    db.add(RevisionCommissionMember(commission_id=commission.id, person_id=person.id, is_chair=is_chair))
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    return person, commission


# ---------------------------------------------------------------------------
# Права
# ---------------------------------------------------------------------------

def test_anonymous_cannot_list_messages(client, db):
    resp = client.get("/revision-chat/messages")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_plain_member_not_in_commission_cannot_access(db, client):
    person = make_person(db, full_name="Постороннев Посторон Посторонович")
    make_user(db, "outsider1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "outsider1", "pass1234")

    resp = client.get("/revision-chat/messages")
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/revision-chat/messages"

    resp = client.post("/revision-chat/messages", data={"body": "тест"})
    assert resp.status_code == 302
    assert db.query(RevisionChatMessage).count() == 0


def test_commission_member_can_list_and_send(db, client):
    _commission_member(db, "rev1")
    login(client, "rev1", "pass1234")

    resp = client.get("/revision-chat/messages")
    assert resp.status_code == 200

    resp = client.post("/revision-chat/messages", data={"body": "привет"})
    assert resp.status_code == 200
    assert db.query(RevisionChatMessage).count() == 1


def test_member_of_closed_commission_cannot_access(db, client):
    """Закрытая (прошлая) комиссия — не текущая, доступа нет."""
    _commission_member(db, "rev_old", closed=True)
    login(client, "rev_old", "pass1234")

    resp = client.get("/revision-chat/messages")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Отправка / список
# ---------------------------------------------------------------------------

def test_send_message_records_author(db, client):
    _commission_member(db, "rev2", full_name="Ревизорова Рева Ревовна")
    login(client, "rev2", "pass1234")

    resp = client.post("/revision-chat/messages", data={"body": "сообщение"})
    data = resp.get_json()
    assert data["message"]["body"] == "сообщение"
    assert data["message"]["author_name"] == "Ревизорова Рева Ревовна"

    user = db.query(User).filter_by(username="rev2").one()
    message = db.query(RevisionChatMessage).one()
    assert message.author_id == user.id


def test_send_empty_message_is_rejected(db, client):
    _commission_member(db, "rev3")
    login(client, "rev3", "pass1234")

    resp = client.post("/revision-chat/messages", data={"body": "   "})
    assert resp.status_code == 400
    assert db.query(RevisionChatMessage).count() == 0


def test_list_messages_after_id_returns_only_newer(db, client):
    _commission_member(db, "rev4")
    login(client, "rev4", "pass1234")

    first = client.post("/revision-chat/messages", data={"body": "первое"}).get_json()["message"]
    client.post("/revision-chat/messages", data={"body": "второе"})

    resp = client.get(f"/revision-chat/messages?after_id={first['id']}")
    bodies = [m["body"] for m in resp.get_json()["messages"]]
    assert bodies == ["второе"]


# ---------------------------------------------------------------------------
# Независимость от чата правления
# ---------------------------------------------------------------------------

def test_board_and_commission_chats_are_independent(db, client):
    """Член правления, не входящий в ревизионную комиссию, не должен
    получить доступ к чату комиссии — и наоборот, сообщения одного чата не
    попадают в другой."""
    board_person = make_person(db, full_name="Правленцев Прав Правленцевич")
    make_user(db, "board_only", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()
    login(client, "board_only", "pass1234")

    resp = client.get("/revision-chat/messages")
    assert resp.status_code == 302

    client.post("/board-chat/messages", data={"body": "в чат правления"})
    client.get("/auth/logout")

    _commission_member(db, "rev5")
    login(client, "rev5", "pass1234")
    resp = client.get("/board-chat/messages")
    assert resp.status_code == 302  # член комиссии, но не правления

    resp = client.get("/revision-chat/messages")
    assert resp.get_json()["messages"] == []  # сообщение из чата правления сюда не попало


# ---------------------------------------------------------------------------
# Бейдж непрочитанных (revision_chat_unread_count)
# ---------------------------------------------------------------------------

def test_widget_present_only_for_commission_member(db, client):
    _commission_member(db, "rev6")
    login(client, "rev6", "pass1234")
    resp = client.get("/cabinet/garages")
    body = resp.get_data(as_text=True)
    assert 'id="revisionChatWidget"' in body
    assert 'id="boardChatWidget"' not in body


def test_widget_absent_for_unrelated_member(db, client):
    person = make_person(db, full_name="Никакойев Никак Никакович")
    make_user(db, "norev1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "norev1", "pass1234")

    resp = client.get("/cabinet/garages")
    body = resp.get_data(as_text=True)
    assert 'id="revisionChatWidget"' not in body


def test_polling_marks_as_read_and_clears_badge(db, client):
    _commission_member(db, "rev7", full_name="Первый Ревизор")
    login(client, "rev7", "pass1234")
    client.post("/revision-chat/messages", data={"body": "от первого"})
    client.get("/auth/logout")

    _commission_member(db, "rev8", full_name="Второй Ревизор")
    login(client, "rev8", "pass1234")
    resp = client.get("/cabinet/garages")
    assert ">1</span>" in resp.get_data(as_text=True)

    client.get("/revision-chat/messages")
    user8 = db.query(User).filter_by(username="rev8").one()
    assert user8.revision_chat_read_at is not None

    resp = client.get("/cabinet/garages")
    assert 'class="chat-widget-badge d-none"' in resp.get_data(as_text=True)
