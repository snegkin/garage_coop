"""
Клиент SMS Aero (app/sms/smsaero.py) и фабрика get_sms_client (app/sms/__init__.py)
— HTTP-запрос мокается (requests.post), реальной отправки в тестах нет.
"""
from unittest.mock import patch, MagicMock

import pytest

from app.sms import get_sms_client, SmsError
from app.sms.smsaero import SmsAeroClient
from app.bank_api import crypto
from app.models import SmsSettings, SmsProvider


def _fake_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def test_send_success():
    client = SmsAeroClient("me@example.com", "apikey123", sign="COOP")
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": True})) as mock_post:
        client.send("9991234567", "hello")

    args, kwargs = mock_post.call_args
    assert args[0] == "https://gate.smsaero.ru/v2/sms/send"
    assert kwargs["auth"] == ("me@example.com", "apikey123")
    assert kwargs["data"]["number"] == "79991234567"  # достроен код страны "7"
    assert kwargs["data"]["text"] == "hello"
    assert kwargs["data"]["sign"] == "COOP"


def test_send_without_sign_omits_it():
    client = SmsAeroClient("me@example.com", "apikey123")
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": True})) as mock_post:
        client.send("9991234567", "hello")
    assert "sign" not in mock_post.call_args.kwargs["data"]


def test_send_raises_on_provider_failure():
    client = SmsAeroClient("me@example.com", "apikey123")
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no money"})):
        with pytest.raises(SmsError, match="no money"):
            client.send("9991234567", "hello")


def test_send_raises_on_network_error():
    import requests
    client = SmsAeroClient("me@example.com", "apikey123")
    with patch("app.sms.smsaero.requests.post", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(SmsError):
            client.send("9991234567", "hello")


def test_send_raises_on_unparsable_response():
    client = SmsAeroClient("me@example.com", "apikey123")
    bad_resp = MagicMock()
    bad_resp.status_code = 502
    bad_resp.json.side_effect = ValueError("not json")
    with patch("app.sms.smsaero.requests.post", return_value=bad_resp):
        with pytest.raises(SmsError):
            client.send("9991234567", "hello")


# ---------------------------------------------------------------------------
# get_sms_client — фабрика
# ---------------------------------------------------------------------------

def test_get_sms_client_returns_none_when_settings_missing():
    assert get_sms_client(None) is None


def test_get_sms_client_returns_none_when_not_configured(app):
    with app.app_context():
        settings = SmsSettings(provider=SmsProvider.SMSAERO)
        assert get_sms_client(settings) is None


def test_get_sms_client_returns_client_when_configured(app):
    with app.app_context():
        settings = SmsSettings(
            provider=SmsProvider.SMSAERO,
            smsaero_email="me@example.com",
            smsaero_api_key_encrypted=crypto.encrypt("apikey123"),
            sender_sign="COOP",
        )
        client = get_sms_client(settings)
        assert isinstance(client, SmsAeroClient)
        assert client.email == "me@example.com"
        assert client.api_key == "apikey123"
        assert client.sign == "COOP"
