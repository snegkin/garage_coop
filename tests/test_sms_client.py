"""
Клиент SMS Aero (app/sms/smsaero.py) и фабрика get_sms_client (app/sms/__init__.py)
— HTTP-запрос мокается (requests.post), реальной отправки в тестах нет.
"""
from unittest.mock import patch, MagicMock

import pytest

from app.sms import get_sms_client, SmsError
from app.sms.smsaero import SmsAeroClient
from app.bank_api import crypto
from app.models import SmsSettings, SmsLog, SmsLogStatus, Counterparty, CounterpartyApiProvider, CounterpartyApiCredential
from tests.conftest import make_person


def _make_configured_settings(db, email="me@example.com", api_key="apikey123", sign=None):
    """Креды теперь на карточке контрагента (CounterpartyApiCredential), а
    SmsSettings хранит только ссылку на него (counterparty_id) — см.
    app/sms/__init__.py:get_sms_client."""
    counterparty = Counterparty(name="SMS Aero", api_provider=CounterpartyApiProvider.SMSAERO)
    db.add(counterparty)
    db.flush()
    db.add(CounterpartyApiCredential(
        counterparty_id=counterparty.id, login=email, secret_encrypted=crypto.encrypt(api_key), extra=sign,
    ))
    settings = SmsSettings(counterparty_id=counterparty.id)
    db.add(settings)
    db.flush()
    return settings


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
    # Тело запроса — JSON (json=...), НЕ application/x-www-form-urlencoded
    # (data=...) — API v2 отвечает {"success": false, "message":
    # "Validation error."} на form-encoded тело (проверено на реальном
    # аккаунте), см. docstring smsaero.py.
    assert "data" not in kwargs
    assert kwargs["json"]["number"] == "79991234567"  # достроен код страны "7"
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["sign"] == "COOP"


def test_send_without_configured_sign_uses_provider_default():
    """"sign" — ОБЯЗАТЕЛЬНОЕ поле API (без него реальный аккаунт отвечает
    {"data": {"sign": ["required"]}}) — если председатель не указал своё
    имя отправителя, подставляем встроенный дефолт провайдера."""
    client = SmsAeroClient("me@example.com", "apikey123")
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": True})) as mock_post:
        client.send("9991234567", "hello")
    assert mock_post.call_args.kwargs["json"]["sign"] == "SMS Aero"


def test_send_raises_on_provider_failure():
    client = SmsAeroClient("me@example.com", "apikey123")
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no money"})):
        with pytest.raises(SmsError, match="no money"):
            client.send("9991234567", "hello")


def test_send_raises_with_field_validation_details():
    """Ответ 400 Validation error несёт детали по каждому невалидному
    полю в payload["data"] — без них текст ошибки был просто "Validation
    error." без единой зацепки, что именно не так (реальный случай — так
    и нашли требование поля "sign")."""
    client = SmsAeroClient("me@example.com", "apikey123")
    payload = {"success": False, "message": "Validation error.", "data": {"sign": ["required"]}}
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response(payload, status_code=400)):
        with pytest.raises(SmsError, match=r"Validation error\..*sign.*required"):
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
# get_sms_client — фабрика (креды на карточке контрагента, SmsSettings
# хранит только ссылку — см. app/sms/__init__.py)
# ---------------------------------------------------------------------------

def test_get_sms_client_returns_none_when_settings_missing(db):
    assert get_sms_client() is None


def test_get_sms_client_returns_none_when_no_counterparty_selected(db):
    db.add(SmsSettings())
    db.commit()
    assert get_sms_client() is None


def test_get_sms_client_returns_none_when_counterparty_provider_changed(db):
    """SmsSettings указывает на контрагента, у которого провайдер потом
    сменили на что-то, не умеющее отправлять SMS — не настроено, не падение."""
    counterparty = Counterparty(name="Бывший SMS Aero", api_provider=CounterpartyApiProvider.BEGET)
    db.add(counterparty)
    db.flush()
    db.add(CounterpartyApiCredential(counterparty_id=counterparty.id, login="x", secret_encrypted=crypto.encrypt("y")))
    db.add(SmsSettings(counterparty_id=counterparty.id))
    db.commit()
    assert get_sms_client() is None


def test_get_sms_client_returns_client_when_configured(db):
    _make_configured_settings(db, sign="COOP")
    db.commit()
    client = get_sms_client()
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

def test_successful_send_is_logged(db):
    _make_configured_settings(db)
    db.commit()
    client = get_sms_client()
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": True})):
        client.send("9991234567", "hello")

    log = db.query(SmsLog).one()
    assert log.phone == "9991234567"
    assert log.text == "hello"
    assert log.status == SmsLogStatus.SENT
    assert log.error is None


def test_failed_send_is_logged_with_error_and_still_raises(db):
    _make_configured_settings(db)
    db.commit()
    client = get_sms_client()
    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no money"})):
        with pytest.raises(SmsError):
            client.send("9991234567", "hello")

    log = db.query(SmsLog).one()
    assert log.status == SmsLogStatus.FAILED
    assert "no money" in log.error


def test_failed_send_log_survives_caller_rollback(db):
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
    from app.models import Person
    _make_configured_settings(db)
    db.commit()
    client = get_sms_client()
    person = make_person(db, full_name="Побочный Коммит Побочнович")

    with patch("app.sms.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no money"})):
        with pytest.raises(SmsError):
            client.send("9991234567", "hello")

    db.rollback()  # то же самое, что делает auth.py при SmsError — здесь уже без эффекта

    assert db.query(SmsLog).count() == 1
    assert db.query(Person).filter_by(id=person.id).first() is not None
