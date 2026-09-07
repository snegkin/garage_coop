"""Настройки СМС-провайдера (/sms/) — только председатель, API-ключ хранится
зашифрованным (тот же приём, что App Secret eWeLink/client_secret банка)."""
from unittest.mock import patch

import datetime as dt

from app.models import RoleEnum, SmsSettings, SmsLog, SmsLogStatus
from app.bank_api import crypto

from tests.conftest import make_person, make_user, login


def _chairman(db, username="chair1"):
    person = make_person(db, full_name="Председателев Пред Предович")
    make_user(db, username, "pass12345", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()


def test_anonymous_cannot_access(client, db):
    resp = client.get("/sms/")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_board_member_cannot_access(db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/sms/")
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/sms/"


def test_chairman_can_view_settings_page(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")
    resp = client.get("/sms/")
    assert resp.status_code == 200


def test_save_settings_encrypts_api_key(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")

    resp = client.post("/sms/settings", data={
        "smsaero_email": "me@example.com", "smsaero_api_key": "secretkey123", "sender_sign": "COOP",
    })
    assert resp.status_code == 302

    settings = db.query(SmsSettings).one()
    assert settings.smsaero_email == "me@example.com"
    assert settings.sender_sign == "COOP"
    assert settings.smsaero_api_key_encrypted != "secretkey123"
    assert crypto.decrypt(settings.smsaero_api_key_encrypted) == "secretkey123"


def test_save_settings_blank_api_key_keeps_existing(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")

    client.post("/sms/settings", data={"smsaero_email": "me@example.com", "smsaero_api_key": "secretkey123"})
    client.post("/sms/settings", data={"smsaero_email": "new@example.com", "smsaero_api_key": ""})

    settings = db.query(SmsSettings).one()
    assert settings.smsaero_email == "new@example.com"
    assert crypto.decrypt(settings.smsaero_api_key_encrypted) == "secretkey123"


def test_send_test_requires_configured_provider(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")

    resp = client.post("/sms/test", data={"test_phone": "9001234567"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/sms/"


def test_send_test_success_records_result(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")
    client.post("/sms/settings", data={"smsaero_email": "me@example.com", "smsaero_api_key": "secretkey123"})

    with patch("app.sms_settings.get_sms_client") as mock_factory:
        mock_client = mock_factory.return_value
        client.post("/sms/test", data={"test_phone": "9001234567"})
        mock_client.send.assert_called_once()

    settings = db.query(SmsSettings).one()
    assert settings.last_test_result
    assert settings.last_test_at is not None


def test_send_test_failure_records_error(db, client):
    from app.sms import SmsError
    _chairman(db)
    login(client, "chair1", "pass12345")
    client.post("/sms/settings", data={"smsaero_email": "me@example.com", "smsaero_api_key": "secretkey123"})

    with patch("app.sms_settings.get_sms_client") as mock_factory:
        mock_factory.return_value.send.side_effect = SmsError("insufficient funds")
        client.post("/sms/test", data={"test_phone": "9001234567"})

    settings = db.query(SmsSettings).one()
    assert "insufficient funds" in settings.last_test_result


# ---------------------------------------------------------------------------
# Журнал отправленных СМС (/sms/log)
# ---------------------------------------------------------------------------

def test_sms_log_requires_chairman(db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/sms/log")
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/sms/log"


def test_sms_log_shows_sent_and_failed_entries(db, client):
    _chairman(db)
    db.add(SmsLog(sent_at=dt.datetime.utcnow(), phone="9991234567", text="Код: 123456", status=SmsLogStatus.SENT))
    db.add(SmsLog(
        sent_at=dt.datetime.utcnow(), phone="9997654321", text="Код: 654321",
        status=SmsLogStatus.FAILED, error="insufficient funds",
    ))
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.get("/sms/log")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "9991234567" in body
    assert "9997654321" in body
    assert "insufficient funds" in body


def test_send_test_is_logged_via_real_client(db, client):
    """Реальная тестовая отправка (без подмены get_sms_client целиком, как
    в test_send_test_success_records_result выше) должна попасть в
    журнал — именно это и есть основной путь диагностики «SMS не
    приходят»."""
    _chairman(db)
    login(client, "chair1", "pass12345")
    client.post("/sms/settings", data={"smsaero_email": "me@example.com", "smsaero_api_key": "secretkey123"})

    with patch("app.sms.smsaero.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"success": True}
        client.post("/sms/test", data={"test_phone": "9001234567"})

    log_entry = db.query(SmsLog).one()
    assert log_entry.status == SmsLogStatus.SENT
    assert log_entry.phone == "9001234567"
