"""
Клиент SMS Aero (app/sms/smsaero.py) и фабрика get_sms_client (app/sms/__init__.py)
— HTTP-запрос мокается (requests.post), реальной отправки в тестах нет.
"""
from unittest.mock import patch, MagicMock

import pytest

from app.sms import get_sms_client, SmsError
from app.sms.smsaero import SmsAeroClient
from app.bank_api import crypto
from app.models import SmsSettings, SmsProvider, SmsLog, SmsLogStatus
from tests.conftest import make_person


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
        # Обёрнут в _LoggingSmsClient (см. SmsLog ниже) — реальный
        # SmsAeroClient доступен через ._inner.
        inner = client._inner
        assert isinstance(inner, SmsAeroClient)
        assert inner.email == "me@example.com"
        assert inner.api_key == "apikey123"
        assert inner.sign == "COOP"


# ---------------------------------------------------------------------------
# Журнал отправленных SMS (SmsLog) — см. app/sms/__init__.py: _LoggingSmsClient
# ---------------------------------------------------------------------------

def _configured_client(app):
    settings = SmsSettings(
        provider=SmsProvider.SMSAERO,
        smsaero_email="me@example.com",
        smsaero_api_key_encrypted=crypto.encrypt("apikey123"),
    )
    return get_sms_client(settings)


def test_successful_send_is_logged(app, db):
    with app.app_context():
        client = _configured_client(app)
        with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": True})):
            client.send("9991234567", "hello")

        log = db.query(SmsLog).one()
        assert log.phone == "9991234567"
        assert log.text == "hello"
        assert log.status == SmsLogStatus.SENT
        assert log.error is None


def test_failed_send_is_logged_with_error_and_still_raises(app, db):
    with app.app_context():
        client = _configured_client(app)
        with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no money"})):
            with pytest.raises(SmsError):
                client.send("9991234567", "hello")

        log = db.query(SmsLog).one()
        assert log.status == SmsLogStatus.FAILED
        assert "no money" in log.error


def test_failed_send_log_survives_caller_rollback(app, db):
    """Ключевое свойство: запись о неудачной отправке коммитится
    НЕЗАВИСИМО от того, что вызывающий код (auth.py) откатывает свою
    транзакцию при ошибке SMS (см. _LoggingSmsClient) — иначе лог не
    показывал бы как раз самые интересные случаи. SQLite допускает только
    одного писателя одновременно, поэтому лог пишется прямо в текущую
    сессию (не через отдельное соединение) — значит send() коммитит
    сессию ЦЕЛИКОМ, а не только запись лога: то, что уже было добавлено
    в сессию до вызова send() (напр. auth.issue_code() — сам одноразовый
    код), окажется закоммичено вместе с ней и переживёт последующий
    rollback() вызывающей стороны. Для реальных путей вызова это
    ожидаемо и безобидно — сам одноразовый код должен быть создан
    независимо от того, дошло ли SMS до адресата (не доставленный код
    просто истечёт через 10 минут или будет заменён повторной попыткой)."""
    with app.app_context():
        client = _configured_client(app)
        from app.models import Person
        person = make_person(db, full_name="Побочный Коммит Побочнович")

        with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no money"})):
            with pytest.raises(SmsError):
                client.send("9991234567", "hello")

        db.rollback()  # то же самое, что делает auth.py при SmsError — здесь уже без эффекта

        assert db.query(SmsLog).count() == 1
        assert db.query(Person).filter_by(id=person.id).first() is not None
