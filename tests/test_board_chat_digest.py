"""
Уведомление правлению о непрочитанных сообщениях в чате правления —
app/notifications.py:run_board_chat_digest (сама логика; scripts/board_chat_digest.py
— тонкая cron-обёртка, та же схема, что у app/penalty.py/scripts/accrue_penalty.py).
"""
import datetime as dt

from app import database, mail_client
from app.models import RoleEnum, BoardChatMessage, NotificationChannel
from app.notifications import run_board_chat_digest

from tests.conftest import make_person, make_user, login
from tests.test_notifications import FakeSmtpConn, _mock_smtp, _make_mailbox_settings


def _board_subscriber(db, username="board_sub", email="board@example.com"):
    person = make_person(db, full_name="Правленец", email=email)
    user = make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    user.notify_channel = NotificationChannel.EMAIL
    user.notify_board_chat = True
    db.flush()
    return user


def _post_message(db, author, body="Сообщение", age=None):
    message = BoardChatMessage(author_id=author.id, body=body)
    db.add(message)
    db.flush()
    if age is not None:
        message.created_at = dt.datetime.utcnow() - age
        db.flush()
    return message


def test_no_notification_for_recent_message(app, db, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    subscriber = _board_subscriber(db)
    _post_message(db, subscriber, age=dt.timedelta(minutes=2))
    db.commit()

    notified = run_board_chat_digest()
    assert notified == 0
    assert len(fake_smtp.sent) == 0


def test_notifies_once_for_unread_message_past_threshold(app, db, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    author = _board_subscriber(db, "author", "author@example.com")
    author.notify_board_chat = False  # автор сообщения — не проверяем в этом тесте
    reader = _board_subscriber(db, "reader", "reader@example.com")
    _post_message(db, author, age=dt.timedelta(minutes=15))
    db.commit()

    notified = run_board_chat_digest()
    assert notified == 1
    assert len(fake_smtp.sent) == 1
    assert "reader@example.com" in fake_smtp.sent[0]["To"]

    # Повторный прогон без нового сообщения — уведомление не дублируется.
    fake_smtp.sent.clear()
    notified_again = run_board_chat_digest()
    assert notified_again == 0
    assert len(fake_smtp.sent) == 0


def test_no_notification_after_message_read(app, db, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    author = _board_subscriber(db, "author2", "author2@example.com")
    author.notify_board_chat = False  # автор сообщения — не проверяем в этом тесте
    reader = _board_subscriber(db, "reader2", "reader2@example.com")
    message = _post_message(db, author, age=dt.timedelta(minutes=15))
    reader.board_chat_read_at = dt.datetime.utcnow()
    db.commit()

    notified = run_board_chat_digest()
    assert notified == 0
    assert len(fake_smtp.sent) == 0


def test_non_board_user_not_notified(app, db, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    author = _board_subscriber(db, "author3", "author3@example.com")
    author.notify_board_chat = False  # автор сообщения — не проверяем в этом тесте
    member_person = make_person(db, full_name="Рядовой", email="rank@example.com")
    member = make_user(db, "rankmember2", "pass1234", role=RoleEnum.MEMBER, person=member_person)
    member.notify_channel = NotificationChannel.EMAIL
    member.notify_board_chat = True  # гипотетически выставлено напрямую в БД
    _post_message(db, author, age=dt.timedelta(minutes=15))
    db.commit()

    notified = run_board_chat_digest()
    assert notified == 0
    assert len(fake_smtp.sent) == 0
