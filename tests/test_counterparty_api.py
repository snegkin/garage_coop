"""
Тесты интеграции с API контрагента (app/counterparty_api/,
app/counterparty_sync.py) — по образцу tests/test_bank_sync.py (стаб-клиент
+ monkeypatch фабрики) и tests/test_sms_client.py (мок requests.post).
Реальные сетевые запросы к SMS Aero не делаются.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from app import database
from app import counterparty_sync
from app.bank_api import crypto
from app.bank_api.base import BalanceInfo
from app.counterparty_api import get_client
from app.counterparty_api.base import CounterpartyApiError
from app.counterparty_api.smsaero import SmsAeroBalanceClient
from app.counterparty_api.beget import BegetBalanceClient
from app.counterparty_api import tns_energo_business
from app.counterparty_api.tns_energo_business import (
    TnsEnergoBusinessBalanceClient, _extract_rsa_params, _build_rsa_plaintext, _rsa_encrypt, _extract_balance,
)
from app.models import RoleEnum, Counterparty, CounterpartyApiProvider, CounterpartyApiCredential, AuditLog

from tests.conftest import make_user, login


def _make_counterparty(db, provider=CounterpartyApiProvider.NONE, name="ООО Ромашка"):
    c = Counterparty(name=name, api_provider=provider)
    db.add(c)
    db.flush()
    return c


def _make_credential(db, counterparty, login="me@example.com", secret="apikey123", extra=None):
    cred = CounterpartyApiCredential(
        counterparty_id=counterparty.id, login=login,
        secret_encrypted=crypto.encrypt(secret) if secret else None, extra=extra,
    )
    db.add(cred)
    db.flush()
    return cred


def _fake_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# SmsAeroBalanceClient.get_balance() — без сети
# ---------------------------------------------------------------------------

def test_get_balance_success():
    client = SmsAeroBalanceClient("me@example.com", "apikey123")
    with patch("app.counterparty_api.smsaero.requests.post", return_value=_fake_response({"success": True, "data": {"balance": 337.03}})) as mock_post:
        info = client.get_balance()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://gate.smsaero.ru/v2/balance"
    assert kwargs["auth"] == ("me@example.com", "apikey123")
    assert info.amount == Decimal("337.03")
    assert info.as_of == dt.date.today()


def test_get_balance_raises_on_provider_failure():
    client = SmsAeroBalanceClient("me@example.com", "apikey123")
    with patch("app.counterparty_api.smsaero.requests.post", return_value=_fake_response({"success": False, "message": "no access"})):
        with pytest.raises(CounterpartyApiError, match="no access"):
            client.get_balance()


def test_get_balance_raises_on_network_error():
    import requests
    client = SmsAeroBalanceClient("me@example.com", "apikey123")
    with patch("app.counterparty_api.smsaero.requests.post", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(CounterpartyApiError):
            client.get_balance()


def test_get_balance_raises_on_unparsable_response():
    client = SmsAeroBalanceClient("me@example.com", "apikey123")
    bad_resp = MagicMock()
    bad_resp.status_code = 502
    bad_resp.json.side_effect = ValueError("not json")
    with patch("app.counterparty_api.smsaero.requests.post", return_value=bad_resp):
        with pytest.raises(CounterpartyApiError):
            client.get_balance()


def test_get_balance_raises_when_balance_field_missing():
    client = SmsAeroBalanceClient("me@example.com", "apikey123")
    with patch("app.counterparty_api.smsaero.requests.post", return_value=_fake_response({"success": True, "data": {}})):
        with pytest.raises(CounterpartyApiError):
            client.get_balance()


# ---------------------------------------------------------------------------
# BegetBalanceClient.get_balance() — без сети
# ---------------------------------------------------------------------------

def test_beget_get_balance_success():
    client = BegetBalanceClient("mylogin", "mypassword")
    payload = {"status": "success", "answer": {"status": "success", "result": {"user_balance": 337.03}}}
    with patch("app.counterparty_api.beget.requests.get", return_value=_fake_response(payload)) as mock_get:
        info = client.get_balance()
    args, kwargs = mock_get.call_args
    assert args[0] == "https://api.beget.com/api/user/getAccountInfo"
    assert kwargs["params"] == {"login": "mylogin", "passwd": "mypassword", "output_format": "json"}
    assert info.amount == Decimal("337.03")
    assert info.as_of == dt.date.today()


def test_beget_get_balance_raises_on_top_level_error():
    client = BegetBalanceClient("mylogin", "wrongpass")
    payload = {"status": "error", "error_text": "Authorization error", "error_code": "AUTH_ERROR"}
    with patch("app.counterparty_api.beget.requests.get", return_value=_fake_response(payload)):
        with pytest.raises(CounterpartyApiError, match="Authorization error"):
            client.get_balance()


def test_beget_get_balance_raises_on_nested_answer_error():
    client = BegetBalanceClient("mylogin", "mypassword")
    payload = {"status": "success", "answer": {"status": "error", "errors": [{"error_code": "INVALID_DATA", "error_text": "Login length cannot be greater than 12 characters"}]}}
    with patch("app.counterparty_api.beget.requests.get", return_value=_fake_response(payload)):
        with pytest.raises(CounterpartyApiError, match="Login length"):
            client.get_balance()


def test_beget_get_balance_raises_on_network_error():
    import requests
    client = BegetBalanceClient("mylogin", "mypassword")
    with patch("app.counterparty_api.beget.requests.get", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(CounterpartyApiError):
            client.get_balance()


def test_beget_get_balance_raises_on_unparsable_response():
    client = BegetBalanceClient("mylogin", "mypassword")
    bad_resp = MagicMock()
    bad_resp.status_code = 502
    bad_resp.json.side_effect = ValueError("not json")
    with patch("app.counterparty_api.beget.requests.get", return_value=bad_resp):
        with pytest.raises(CounterpartyApiError):
            client.get_balance()


def test_beget_get_balance_does_not_leak_password_in_error():
    """Пароль идёт GET-параметром — текст ошибки должен браться из тела
    ответа, а не из URL/объекта запроса (иначе секрет может утечь в
    flash-сообщение или лог)."""
    client = BegetBalanceClient("mylogin", "super-secret-password")
    payload = {"status": "error", "error_text": "Authorization error", "error_code": "AUTH_ERROR"}
    with patch("app.counterparty_api.beget.requests.get", return_value=_fake_response(payload)):
        try:
            client.get_balance()
        except CounterpartyApiError as exc:
            assert "super-secret-password" not in str(exc)


# ---------------------------------------------------------------------------
# get_client — фабрика (оба провайдера читают CounterpartyApiCredential одинаково)
# ---------------------------------------------------------------------------

def test_get_client_none_when_provider_is_none(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.NONE)
    assert get_client(counterparty) is None


def test_get_client_none_without_credential(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    assert get_client(counterparty) is None


def test_get_client_none_without_secret(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    _make_credential(db, counterparty, login="me@example.com", secret=None)
    db.commit()
    assert get_client(counterparty) is None


def test_get_client_none_on_decrypt_failure(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    cred = CounterpartyApiCredential(counterparty_id=counterparty.id, login="me@example.com", secret_encrypted="not-a-valid-fernet-token")
    db.add(cred)
    db.commit()
    assert get_client(counterparty) is None


def test_get_client_returns_smsaero_client_when_configured(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    _make_credential(db, counterparty, login="me@example.com", secret="apikey123")
    db.commit()
    client = get_client(counterparty)
    assert isinstance(client, SmsAeroBalanceClient)
    assert client.email == "me@example.com"
    assert client.api_key == "apikey123"


def test_get_client_returns_beget_client_when_configured(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.BEGET)
    _make_credential(db, counterparty, login="mylogin", secret="mypassword")
    db.commit()
    client = get_client(counterparty)
    assert isinstance(client, BegetBalanceClient)
    assert client.login == "mylogin"
    assert client.password == "mypassword"


# ---------------------------------------------------------------------------
# sync_counterparty_balance — с подменённым клиентом
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self, balance_result=None, balance_error=None):
        self._balance_result = balance_result
        self._balance_error = balance_error

    def get_balance(self):
        if self._balance_error:
            raise self._balance_error
        return self._balance_result


def test_sync_counterparty_balance_unsupported(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.NONE)
    db.commit()
    status, message = counterparty_sync.sync_counterparty_balance(counterparty)
    assert status == "unsupported"


def test_sync_counterparty_balance_updates_fields(db, monkeypatch):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    db.commit()

    stub = _StubClient(balance_result=BalanceInfo(amount=Decimal("337.03"), as_of=dt.date(2026, 9, 14)))
    monkeypatch.setattr(counterparty_sync, "get_client", lambda cp: stub)

    status, message = counterparty_sync.sync_counterparty_balance(counterparty)
    assert status == "success"
    db.expire_all()
    updated = database.db_session.get(Counterparty, counterparty.id)
    assert updated.external_balance == Decimal("337.03")
    assert updated.external_balance_updated_at is not None
    assert updated.external_balance_error is None
    log = db.query(AuditLog).filter_by(action="counterparty_api.balance_sync").one()
    assert log.entity_id == counterparty.id


def test_sync_counterparty_balance_error_is_recorded(db, monkeypatch):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    db.commit()

    stub = _StubClient(balance_error=CounterpartyApiError("SMS Aero недоступен"))
    monkeypatch.setattr(counterparty_sync, "get_client", lambda cp: stub)

    status, message = counterparty_sync.sync_counterparty_balance(counterparty)
    assert status == "error"
    db.expire_all()
    updated = database.db_session.get(Counterparty, counterparty.id)
    assert updated.external_balance is None  # не изменился
    assert "SMS Aero недоступен" in updated.external_balance_error


# ---------------------------------------------------------------------------
# Роут — доступен правлению, не рядовому члену
# ---------------------------------------------------------------------------

def test_sync_balance_route_forbidden_for_member(app, db, client, monkeypatch):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    stub = _StubClient(balance_result=BalanceInfo(amount=Decimal("1.00"), as_of=dt.date(2026, 9, 14)))
    monkeypatch.setattr(counterparty_sync, "get_client", lambda cp: stub)

    resp = client.post(f"/counterparties/{counterparty.id}/sync-balance")
    assert resp.status_code == 302  # roles_required редиректит на дашборд, не 403
    db.expire_all()
    updated = database.db_session.get(Counterparty, counterparty.id)
    assert updated.external_balance is None  # не изменился — доступ не дошёл до самой синхронизации


def test_sync_balance_route_updates_counterparty(app, db, client, monkeypatch):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    stub = _StubClient(balance_result=BalanceInfo(amount=Decimal("100.50"), as_of=dt.date(2026, 9, 14)))
    monkeypatch.setattr(counterparty_sync, "get_client", lambda cp: stub)

    resp = client.post(f"/counterparties/{counterparty.id}/sync-balance")
    assert resp.status_code == 302
    db.expire_all()
    updated = database.db_session.get(Counterparty, counterparty.id)
    assert updated.external_balance == Decimal("100.50")


# ---------------------------------------------------------------------------
# Роут «Настроить API» — единая точка для всех провайдеров (app/counterparties.py)
# ---------------------------------------------------------------------------

def test_save_api_credential_creates_and_encrypts(db, client):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.BEGET)
    make_user(db, "board2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board2", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/api-credential", data={
        "login": "mylogin", "secret": "mypassword",
    })
    assert resp.status_code == 302
    db.expire_all()
    cred = database.db_session.get(Counterparty, counterparty.id).api_credential
    assert cred.login == "mylogin"
    assert cred.secret_encrypted != "mypassword"
    assert crypto.decrypt(cred.secret_encrypted) == "mypassword"


def test_save_api_credential_blank_secret_keeps_existing(db, client):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.SMSAERO)
    make_user(db, "board3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board3", "pass12345")

    client.post(f"/counterparties/{counterparty.id}/api-credential", data={"login": "old@example.com", "secret": "original-secret"})
    client.post(f"/counterparties/{counterparty.id}/api-credential", data={"login": "new@example.com", "secret": ""})

    db.expire_all()
    cred = database.db_session.get(Counterparty, counterparty.id).api_credential
    assert cred.login == "new@example.com"
    assert crypto.decrypt(cred.secret_encrypted) == "original-secret"


def test_save_api_credential_requires_provider_selected(db, client):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.NONE)
    make_user(db, "board4", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board4", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/api-credential", data={"login": "x", "secret": "y"})
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Counterparty, counterparty.id).api_credential is None


# ---------------------------------------------------------------------------
# Защита от удаления контрагента с настроенным API
# ---------------------------------------------------------------------------

def test_delete_counterparty_with_configured_api_is_blocked(db, client):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.BEGET)
    _make_credential(db, counterparty, login="mylogin", secret="mypassword")
    make_user(db, "board5", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board5", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Counterparty, counterparty.id) is not None


def test_delete_counterparty_without_api_still_works(db, client):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.NONE)
    make_user(db, "board6", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board6", "pass12345")

    resp = client.post(f"/counterparties/{counterparty.id}/delete")
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(Counterparty, counterparty.id) is None


# ---------------------------------------------------------------------------
# ТНС-Энерго Бизнес — самодельная RSA-схема Битрикса (main.rsasecurity)
# без единого реального обращения к tns-e.ru
# ---------------------------------------------------------------------------

def _toy_rsa_key():
    """Настоящий 1024-битный ключ (chunk=128, как на реальном сайте) —
    маленькие игрушечные простые числа дают модуль короче chunk байт, и
    блоки данных превышают модуль (RSA необратимо теряет данные) — на
    реальном сайте это не проблема только потому, что модуль ровно
    chunk-байт (1024 бита)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    priv = key.private_numbers()
    pub = key.public_key().public_numbers()
    return pub.n, pub.e, priv.d


def test_rsa_encrypt_roundtrip_with_real_key_size():
    """Наша схема кодирования (little-endian блоки, base64, пробел между
    ними, переменная длина результата без выравнивания) действительно
    обратима — проверяем расшифровкой вручную тем же алгоритмом, что
    формально описывает rsasec_crypt (biFromRaw/biToRaw), а не просто
    "похоже на JS"."""
    import base64
    n, e, d = _toy_rsa_key()
    chunk = 128
    plaintext = _build_rsa_plaintext("randvalue123", "MyP@ssw0rd! пароль", ["USER_PASSWORD", "USER_CONFIRM_PASSWORD"])

    encrypted = _rsa_encrypt(plaintext, e, n, chunk)

    data = plaintext.encode("utf-8")
    pad = (-len(data)) % chunk
    expected_padded = data + b"\x00" * pad

    decrypted = b""
    for b64chunk in encrypted.split(" "):
        raw = base64.b64decode(b64chunk)
        value = int.from_bytes(raw, "little")
        plain_int = pow(value, d, n)
        decrypted += plain_int.to_bytes(chunk, "little")

    assert decrypted == expected_padded
    assert decrypted.rstrip(b"\x00").decode("utf-8") == plaintext


def test_build_rsa_plaintext_includes_sha1_and_skips_unknown_params():
    import hashlib
    plaintext = _build_rsa_plaintext("rand1", "secret", ["USER_PASSWORD", "USER_CONFIRM_PASSWORD"])
    body, sha = plaintext.rsplit("&__SHA=", 1)
    assert body == "__RSA_RAND=rand1&USER_PASSWORD=secret"
    assert sha == hashlib.sha1(body.encode("utf-8")).hexdigest()


def test_extract_rsa_params_parses_inline_script():
    html = (
        '<script>top.BX.defer(top.rsasec_form_bind)('
        '{"formid":"form_auth","key":{"M":"y0iwmCg=","E":"AQAB","chunk":128},'
        '"rsa_rand":"heeqb1hh1yhbtsof7gbx","params":["USER_PASSWORD","USER_CONFIRM_PASSWORD"]});</script>'
    )
    params = _extract_rsa_params(html)
    assert params["rsa_rand"] == "heeqb1hh1yhbtsof7gbx"
    assert params["key"]["chunk"] == 128
    assert params["params"] == ["USER_PASSWORD", "USER_CONFIRM_PASSWORD"]


def test_extract_rsa_params_raises_when_marker_missing():
    with pytest.raises(CounterpartyApiError):
        _extract_rsa_params("<html>обычная страница без формы входа</html>")


def test_extract_balance_parses_first_formatted_value():
    html = (
        '<div class="formattedValue ">'
        '<span class="formattedValue__main">3 314,09</span>'
        '<span class="formattedValue__currency">руб.</span>'
        "</div>"
    )
    assert _extract_balance(html) == Decimal("3314.09")


def test_extract_balance_returns_none_when_absent():
    assert _extract_balance("<div>ничего нет</div>") is None


class _FakeTnsResponse:
    def __init__(self, text):
        self.text = text


class _FakeTnsSession:
    def __init__(self, login_html, result_html):
        self._login_html = login_html
        self._result_html = result_html
        self.post_calls = []

    def get(self, url, timeout=None):
        return _FakeTnsResponse(self._login_html)

    def post(self, url, data=None, timeout=None):
        self.post_calls.append((url, data))
        return _FakeTnsResponse(self._result_html)


def _login_page_html(rsa_rand="randvalue123"):
    n, e, d = _toy_rsa_key()
    import base64
    m_b64 = base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "little")).decode("ascii")
    e_b64 = base64.b64encode(e.to_bytes((e.bit_length() + 7) // 8, "little")).decode("ascii")
    return (
        '<script>top.BX.defer(top.rsasec_form_bind)('
        '{"formid":"form_auth","key":{"M":"%s","E":"%s","chunk":128},'
        '"rsa_rand":"%s","params":["USER_PASSWORD","USER_CONFIRM_PASSWORD"]});</script>'
    ) % (m_b64, e_b64, rsa_rand)


_BALANCE_PAGE_HTML = (
    '<div class="formattedValue "><span class="formattedValue__main">3 314,09</span>'
    '<span class="formattedValue__currency">руб.</span></div>'
)


def test_get_balance_logs_in_and_inverts_sign(monkeypatch):
    fake_session = _FakeTnsSession(_login_page_html(), _BALANCE_PAGE_HTML)
    monkeypatch.setattr(tns_energo_business.requests, "Session", lambda: fake_session)

    client = TnsEnergoBusinessBalanceClient("me@example.com", "mypassword", "yar")
    info = client.get_balance()

    # Долг на экране ЛК показан положительным числом — в нашей системе
    # (как и у остальных провайдеров) отрицательное значит "мы должны".
    assert info.amount == Decimal("-3314.09")
    assert info.as_of == dt.date.today()

    assert len(fake_session.post_calls) == 1
    url, data = fake_session.post_calls[0]
    assert url == "https://lk-b2b-yar.tns-e.ru/auth/"
    assert data["AUTH_TYPE"] == "LEGAL"
    assert data["AUTH_ACTION"] == "Войти"
    assert data["USER_LOGIN"] == "me@example.com"
    assert data["__RSA_DATA"]  # непустая строка
    assert "mypassword" not in data["__RSA_DATA"]  # пароль зашифрован, не в открытом виде


def test_get_balance_raises_when_login_page_has_no_rsa_params(monkeypatch):
    fake_session = _FakeTnsSession("<html>обычная страница</html>", _BALANCE_PAGE_HTML)
    monkeypatch.setattr(tns_energo_business.requests, "Session", lambda: fake_session)

    client = TnsEnergoBusinessBalanceClient("me@example.com", "mypassword", "yar")
    with pytest.raises(CounterpartyApiError):
        client.get_balance()


def test_get_balance_raises_when_balance_not_found_after_login(monkeypatch):
    """Например, неверный логин/пароль — сервер возвращает страницу без
    виджета баланса (снова форма входа с ошибкой)."""
    fake_session = _FakeTnsSession(_login_page_html(), "<html>Неверный логин или пароль</html>")
    monkeypatch.setattr(tns_energo_business.requests, "Session", lambda: fake_session)

    client = TnsEnergoBusinessBalanceClient("me@example.com", "wrongpassword", "yar")
    with pytest.raises(CounterpartyApiError):
        client.get_balance()


# ---------------------------------------------------------------------------
# get_client — ТНС-Энерго Бизнес требует ещё и регион (cred.extra)
# ---------------------------------------------------------------------------

def test_get_client_none_for_tns_energo_without_region(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.TNS_ENERGO_BUSINESS)
    _make_credential(db, counterparty, login="me@example.com", secret="mypassword", extra=None)
    db.commit()
    assert get_client(counterparty) is None


def test_get_client_returns_tns_energo_client_when_configured(db):
    counterparty = _make_counterparty(db, provider=CounterpartyApiProvider.TNS_ENERGO_BUSINESS)
    _make_credential(db, counterparty, login="me@example.com", secret="mypassword", extra="yar")
    db.commit()
    client = get_client(counterparty)
    assert isinstance(client, TnsEnergoBusinessBalanceClient)
    assert client.email == "me@example.com"
    assert client.password == "mypassword"
    assert client.region == "yar"
    assert client.base_url == "https://lk-b2b-yar.tns-e.ru"
