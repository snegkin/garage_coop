"""
Восстановление пароля по email или телефону (auth.forgot_password/
reset_password) — канал определяется по виду введённого идентификатора.
reCAPTCHA пропускается в тестах (RECAPTCHA_SITE_KEY/SECRET_KEY не заданы
в TestConfig — тот же дев-фолбэк, что и в реальном приложении, см.
app/recaptcha.py:verify).
"""
import re
from unittest.mock import patch

import pytest
from werkzeug.security import check_password_hash

from app.models import User, MailboxSettings
from tests.conftest import make_person, make_user


def _configure_mailbox(db):
    settings = MailboxSettings(incoming_host="imap.example.com", username="board@example.com", password_encrypted="x")
    db.add(settings)
    db.commit()
    return settings


class _FakeSmsClient:
    def __init__(self):
        self.sent = []

    def send(self, phone_digits, text):
        self.sent.append((phone_digits, text))

    def last_code(self):
        match = re.search(r"\d{6}", self.sent[-1][1])
        assert match
        return match.group(0)


@pytest.fixture()
def fake_sms(monkeypatch):
    client = _FakeSmsClient()
    monkeypatch.setattr("app.auth.get_sms_client", lambda settings: client)
    return client


def _last_code_from_email(mock_send):
    body = mock_send.call_args.kwargs["body_text"]
    match = re.search(r"\d{6}", body)
    assert match
    return match.group(0)


# ---------------------------------------------------------------------------
# Восстановление по email
# ---------------------------------------------------------------------------

def test_forgot_password_by_email_sends_code(db, client):
    _configure_mailbox(db)
    person = make_person(db, full_name="Иванов Иван Иванович", email="ivan@example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    with patch("app.mail_client.send_message") as mock_send:
        resp = client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})
    assert resp.status_code == 302
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["to_addrs"] == ["ivan@example.com"]


def test_reset_password_by_email_full_flow(db, client):
    _configure_mailbox(db)
    person = make_person(db, full_name="Иванов Иван Иванович", email="ivan@example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    with patch("app.mail_client.send_message") as mock_send:
        client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})
    code = _last_code_from_email(mock_send)

    resp = client.post("/auth/reset-password", data={
        "target": "ivan@example.com", "code": code,
        "new_password": "newpassword1", "confirm_password": "newpassword1",
    })
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/auth/login"

    user = db.query(User).filter_by(person_id=person.id).one()
    assert check_password_hash(user.password_hash, "newpassword1")
    assert not check_password_hash(user.password_hash, "oldpassword")


def test_forgot_password_email_case_insensitive(db, client):
    _configure_mailbox(db)
    person = make_person(db, full_name="Иванов Иван Иванович", email="Ivan@Example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    with patch("app.mail_client.send_message") as mock_send:
        resp = client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})
    assert resp.status_code == 302
    mock_send.assert_called_once()


def test_forgot_password_email_without_mailbox_configured_shows_generic_message(db, client):
    """Почта не настроена — тот же общий ответ, что и "не нашли", без
    падения и без явного указания причины."""
    person = make_person(db, full_name="Иванов Иван Иванович", email="ivan@example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    resp = client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Восстановление по телефону (СМС)
# ---------------------------------------------------------------------------

def test_reset_password_by_phone_full_flow(db, client, fake_sms):
    from app.models import Phone
    person = make_person(db, full_name="Петров Пётр Петрович")
    db.add(Phone(person_id=person.id, number="+7 900 111-22-33"))
    make_user(db, "petrov1", "oldpassword", person=person)
    db.commit()

    resp = client.post("/auth/forgot-password", data={"identifier": "89001112233"})
    assert resp.status_code == 302
    code = fake_sms.last_code()

    reset_resp = client.post("/auth/reset-password", data={
        "target": "89001112233", "code": code,
        "new_password": "newpassword1", "confirm_password": "newpassword1",
    })
    assert reset_resp.status_code == 302
    assert reset_resp.headers["Location"] == "/auth/login"

    user = db.query(User).filter_by(person_id=person.id).one()
    assert check_password_hash(user.password_hash, "newpassword1")


# ---------------------------------------------------------------------------
# Не найдено / ошибки
# ---------------------------------------------------------------------------

def test_forgot_password_unknown_identifier_shows_generic_message_and_sends_nothing(db, client):
    _configure_mailbox(db)
    with patch("app.mail_client.send_message") as mock_send:
        resp = client.post("/auth/forgot-password", data={"identifier": "nobody@example.com"})
    assert resp.status_code == 302
    mock_send.assert_not_called()


def test_forgot_password_person_without_account_sends_nothing(db, client):
    """Person найден по email, но у него нет User — тоже общий ответ, письмо не шлём."""
    _configure_mailbox(db)
    make_person(db, full_name="Без Аккаунта Безаккаунтович", email="noaccount@example.com")
    db.commit()

    with patch("app.mail_client.send_message") as mock_send:
        resp = client.post("/auth/forgot-password", data={"identifier": "noaccount@example.com"})
    assert resp.status_code == 302
    mock_send.assert_not_called()


def test_forgot_password_missing_identifier(client, db):
    resp = client.post("/auth/forgot-password", data={"identifier": ""})
    assert resp.status_code == 200  # форма перерисовывается со флэшем, не редирект


def test_reset_password_wrong_code(db, client):
    _configure_mailbox(db)
    person = make_person(db, full_name="Иванов Иван Иванович", email="ivan@example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    with patch("app.mail_client.send_message"):
        client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})

    resp = client.post("/auth/reset-password", data={
        "target": "ivan@example.com", "code": "000000",
        "new_password": "newpassword1", "confirm_password": "newpassword1",
    })
    assert resp.status_code == 200
    user = db.query(User).filter_by(person_id=person.id).one()
    assert check_password_hash(user.password_hash, "oldpassword")  # не изменился


def test_reset_password_mismatched_confirmation(db, client):
    _configure_mailbox(db)
    person = make_person(db, full_name="Иванов Иван Иванович", email="ivan@example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    with patch("app.mail_client.send_message") as mock_send:
        client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})
    code = _last_code_from_email(mock_send)

    resp = client.post("/auth/reset-password", data={
        "target": "ivan@example.com", "code": code,
        "new_password": "newpassword1", "confirm_password": "different1",
    })
    assert resp.status_code == 200
    user = db.query(User).filter_by(person_id=person.id).one()
    assert check_password_hash(user.password_hash, "oldpassword")


def test_reset_password_too_short(db, client):
    _configure_mailbox(db)
    person = make_person(db, full_name="Иванов Иван Иванович", email="ivan@example.com")
    make_user(db, "ivanov1", "oldpassword", person=person)
    db.commit()

    with patch("app.mail_client.send_message") as mock_send:
        client.post("/auth/forgot-password", data={"identifier": "ivan@example.com"})
    code = _last_code_from_email(mock_send)

    resp = client.post("/auth/reset-password", data={
        "target": "ivan@example.com", "code": code,
        "new_password": "ab", "confirm_password": "ab",
    })
    assert resp.status_code == 200
    user = db.query(User).filter_by(person_id=person.id).one()
    assert check_password_hash(user.password_hash, "oldpassword")
