"""Настройки СМС (/sms/) — только председатель. Сама страница хранит не
email/API-ключ (те — на карточке контрагента, CounterpartyApiCredential,
см. app/counterparties.py), а только выбор контрагента, обслуживающего
отправку (SmsSettings.counterparty_id, см. app/sms/__init__.py)."""
from unittest.mock import patch

import datetime as dt

from app.models import RoleEnum, SmsSettings, SmsLog, SmsLogStatus, Counterparty, CounterpartyApiProvider, CounterpartyApiCredential
from app.bank_api import crypto

from tests.conftest import make_person, make_user, login


def _chairman(db, username="chair1"):
    person = make_person(db, full_name="Председателев Пред Предович")
    make_user(db, username, "pass12345", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()


def _make_smsaero_counterparty(db, name="SMS Aero", email="me@example.com", api_key="secretkey123"):
    counterparty = Counterparty(name=name, api_provider=CounterpartyApiProvider.SMSAERO)
    db.add(counterparty)
    db.flush()
    db.add(CounterpartyApiCredential(counterparty_id=counterparty.id, login=email, secret_encrypted=crypto.encrypt(api_key)))
    db.flush()
    return counterparty


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


def test_save_settings_selects_counterparty(db, client):
    _chairman(db)
    counterparty = _make_smsaero_counterparty(db)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.post("/sms/settings", data={"counterparty_id": str(counterparty.id)})
    assert resp.status_code == 302

    settings = db.query(SmsSettings).one()
    assert settings.counterparty_id == counterparty.id


def test_save_settings_can_clear_selection(db, client):
    _chairman(db)
    counterparty = _make_smsaero_counterparty(db)
    db.commit()
    login(client, "chair1", "pass12345")

    client.post("/sms/settings", data={"counterparty_id": str(counterparty.id)})
    client.post("/sms/settings", data={"counterparty_id": ""})

    settings = db.query(SmsSettings).one()
    assert settings.counterparty_id is None


def test_only_sms_capable_counterparties_are_offered(db, client):
    """Контрагент с другим провайдером (например, Beget) не должен
    предлагаться для выбора на /sms/ — он не умеет отправлять SMS."""
    _chairman(db)
    _make_smsaero_counterparty(db, name="SMS Aero аккаунт")
    beget = Counterparty(name="Хостинг Beget", api_provider=CounterpartyApiProvider.BEGET)
    db.add(beget)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.get("/sms/")
    body = resp.get_data(as_text=True)
    assert "SMS Aero аккаунт" in body
    assert "Хостинг Beget" not in body


def test_send_test_requires_configured_provider(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")

    resp = client.post("/sms/test", data={"test_phone": "9001234567"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/sms/"


def test_send_test_success_records_result(db, client):
    _chairman(db)
    login(client, "chair1", "pass12345")

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

    with patch("app.sms_settings.get_sms_client") as mock_factory:
        mock_factory.return_value.send.side_effect = SmsError("insufficient funds")
        client.post("/sms/test", data={"test_phone": "9001234567"})

    settings = db.query(SmsSettings).one()
    assert "insufficient funds" in settings.last_test_result


# ---------------------------------------------------------------------------
# Журнал отправленных СМС — на той же странице /sms/, что и настройки
# (см. sms_settings/page.html: журнал + модалка «Настройки»)
# ---------------------------------------------------------------------------

def test_sms_log_shown_on_settings_page_for_chairman(db, client):
    """Раньше журнал был на отдельной странице /sms/log — объединили с
    /sms/, чтобы не прыгать между страницами при разборе жалоб «SMS не
    приходят»."""
    _chairman(db)
    db.add(SmsLog(sent_at=dt.datetime.utcnow(), phone="9991234567", text="Код: 123456", status=SmsLogStatus.SENT))
    db.add(SmsLog(
        sent_at=dt.datetime.utcnow(), phone="9997654321", text="Код: 654321",
        status=SmsLogStatus.FAILED, error="insufficient funds",
    ))
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.get("/sms/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "9991234567" in body
    assert "9997654321" in body
    assert "insufficient funds" in body


def test_settings_modal_present_on_settings_page(db, client):
    """Настройки провайдера и тестовая отправка — в модальном окне по
    кнопке «Настройки», а не отдельной страницей/формой на весь экран."""
    _chairman(db)
    login(client, "chair1", "pass12345")

    resp = client.get("/sms/")
    body = resp.get_data(as_text=True)
    assert 'id="smsSettingsModal"' in body
    assert 'data-bs-target="#smsSettingsModal"' in body
    assert 'name="counterparty_id"' in body


def test_send_test_is_logged_via_real_client(db, client):
    """Реальная тестовая отправка (без подмены get_sms_client целиком, как
    в test_send_test_success_records_result выше) должна попасть в
    журнал — именно это и есть основной путь диагностики «SMS не
    приходят»."""
    _chairman(db)
    counterparty = _make_smsaero_counterparty(db)
    db.commit()
    login(client, "chair1", "pass12345")
    client.post("/sms/settings", data={"counterparty_id": str(counterparty.id)})

    with patch("app.sms.smsaero.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"success": True}
        client.post("/sms/test", data={"test_phone": "9001234567"})

    log_entry = db.query(SmsLog).one()
    assert log_entry.status == SmsLogStatus.SENT
    assert log_entry.phone == "9001234567"
