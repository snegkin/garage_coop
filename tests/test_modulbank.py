"""
Клиент API Модульбанка (app/bank_api/modulbank.py) и его подключение через
get_client()/настройки API. Без сети: requests.post подменяется фейком,
который отвечает по пути запроса, как описано в спецификации банка
(https://api.modulbank.ru/v1/swagger/docs/v1).
"""
import datetime as dt
from decimal import Decimal

import pytest

from app import database
from app.bank_api import crypto, get_client, modulbank
from app.bank_api.base import BankApiError
from app.bank_api.modulbank import ModulbankClient, _parse_operation
from app.models import RoleEnum, BankAccount, BankApiCredential, BankApiProvider

from tests.conftest import make_user, login

ACC_NUMBER = "40703810770010000001"
ACC_ID = "0f6a3b0e-1111-2222-3333-444455556666"


def make_account(db, sandbox=False, token="real-token"):
    account = BankAccount(bank_name="Модульбанк", checking_account=ACC_NUMBER, api_provider=BankApiProvider.MODULBANK)
    db.add(account)
    db.flush()
    db.add(BankApiCredential(
        bank_account_id=account.id, sandbox=sandbox,
        api_token_encrypted=crypto.encrypt(token) if token else None,
    ))
    db.flush()
    return account


def op(**kw):
    base = {
        "id": "op-1", "status": "Received", "category": "Debet", "currency": "RUR",
        "amount": 1432.56, "contragentName": "Иванов Иван Иванович", "contragentInn": "",
        "paymentPurpose": "ЛС 10640; ЭЛЕКТРОЭНЕРГИЯ", "executed": "2026-11-05T00:00:00",
        "created": "2026-11-04T18:00:00", "docNumber": "17",
    }
    base.update(kw)
    return base


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def fake_bank(monkeypatch):
    """Фейковый банк: account-info с одним нашим счётом, история операций —
    из списка state["operations"], постранично по skip/records."""
    state = {
        "operations": [], "calls": [],
        "accounts": [{"id": ACC_ID, "number": ACC_NUMBER, "currency": "RUR", "balance": 100.0}],
        "balance": 12345.67, "status": 200,
    }

    def fake_post(url, headers=None, json=None, timeout=None):
        state["calls"].append({"url": url, "headers": headers, "json": json})
        if state["status"] != 200:
            return FakeResponse(state["status"], {"exceptionType": "AccessDenied", "message": "нет доступа"})
        path = url.split("/v1", 1)[1]
        if path == "/account-info":
            return FakeResponse(200, [{"companyId": "c1", "bankAccounts": state["accounts"]}])
        if path == f"/account-info/balance/{ACC_ID}":
            return FakeResponse(200, state["balance"])
        if path == f"/operation-history/{ACC_ID}":
            skip, records = json["skip"], json["records"]
            return FakeResponse(200, state["operations"][skip:skip + records])
        return FakeResponse(404, {"message": "not found"})

    monkeypatch.setattr(modulbank.requests, "post", fake_post)
    return state


# ---------------------------------------------------------------------------
# Разбор операции
# ---------------------------------------------------------------------------

def test_parse_operation_debet_is_incoming_credit():
    line = _parse_operation(op())
    assert line.direction == "credit"  # у Модульбанка Debet — входящая
    assert line.amount == Decimal("1432.56")
    assert line.operation_date == dt.date(2026, 11, 5)  # executed, не created
    assert line.external_uid == "op-1"
    assert line.document_number == "17"
    assert line.counterparty_inn is None  # пустая строка → None


def test_parse_operation_credit_is_outgoing_debit():
    line = _parse_operation(op(category="Credit", status="Executed"))
    assert line.direction == "debit"


@pytest.mark.parametrize("status", ["Draft", "InSigning", "SendToBank", "RejectByBank", "Canceled", "ClarifyRequiredOutcome"])
def test_parse_operation_skips_not_executed(status):
    assert _parse_operation(op(status=status)) is None


def test_parse_operation_skips_foreign_currency():
    assert _parse_operation(op(currency="USD")) is None


def test_parse_operation_falls_back_to_created_date():
    assert _parse_operation(op(executed=None)).operation_date == dt.date(2026, 11, 4)


def test_parse_operation_unknown_category_raises():
    with pytest.raises(ValueError):
        _parse_operation(op(category="Something"))


# ---------------------------------------------------------------------------
# Клиент: счёт, баланс, выписка
# ---------------------------------------------------------------------------

def test_get_balance_resolves_account_id_by_number(fake_bank):
    client = ModulbankClient(token="t", account_number="4070 3810 7700 1000 0001")
    info = client.get_balance()
    assert info.amount == Decimal("12345.67")
    assert fake_bank["calls"][0]["headers"]["Authorization"] == "Bearer t"
    assert "sandbox" not in fake_bank["calls"][0]["headers"]
    assert fake_bank["calls"][1]["url"].endswith(f"/account-info/balance/{ACC_ID}")


def test_unknown_account_number_raises_with_known_numbers(fake_bank):
    client = ModulbankClient(token="t", account_number="40703810000000009999")
    with pytest.raises(BankApiError) as e:
        client.get_balance()
    assert ACC_NUMBER in str(e.value)


def test_sandbox_uses_sandbox_token_header_and_first_account(fake_bank):
    fake_bank["accounts"] = [{"id": ACC_ID, "number": "40702810000000000000", "currency": "RUR"}]
    client = ModulbankClient(token="real", account_number=ACC_NUMBER, sandbox=True)
    client.get_balance()
    headers = fake_bank["calls"][0]["headers"]
    assert headers["Authorization"] == "Bearer sandboxtoken"
    assert headers["sandbox"] == "on"


def test_get_statement_paginates_filters_and_dedups(fake_bank):
    ops = [op(id=f"op-{i}", executed="2026-11-05T00:00:00") for i in range(60)]
    ops.append(op(id="op-5"))  # повтор из-за сдвига страниц
    ops.append(op(id="old", executed="2026-10-01T00:00:00"))  # вне периода (песочница фильтр дат игнорирует)
    ops.append(op(id="draft", status="Draft"))
    fake_bank["operations"] = ops
    client = ModulbankClient(token="t", account_number=ACC_NUMBER)
    lines = client.get_statement(dt.date(2026, 11, 1), dt.date(2026, 11, 11))
    assert len(lines) == 60
    history_calls = [c for c in fake_bank["calls"] if "/operation-history/" in c["url"]]
    assert [c["json"]["skip"] for c in history_calls] == [0, 50]
    assert history_calls[0]["json"]["from"] == "2026-11-01"
    assert history_calls[0]["json"]["till"] == "2026-11-12"  # следующий день — чтобы не отрезать последний


def test_unparseable_operation_wrapped_in_bank_api_error(fake_bank):
    fake_bank["operations"] = [op(amount=None)]
    client = ModulbankClient(token="t", account_number=ACC_NUMBER)
    with pytest.raises(BankApiError) as e:
        client.get_statement(dt.date(2026, 11, 1), dt.date(2026, 11, 11))
    assert "op-1" in str(e.value)


def test_401_gives_actionable_message(fake_bank):
    fake_bank["status"] = 401
    client = ModulbankClient(token="t", account_number=ACC_NUMBER)
    with pytest.raises(BankApiError) as e:
        client.get_balance()
    assert "Токен API" in str(e.value)


def test_403_shows_bank_error_text(fake_bank):
    fake_bank["status"] = 403
    client = ModulbankClient(token="t", account_number=ACC_NUMBER)
    with pytest.raises(BankApiError) as e:
        client.get_balance()
    assert "AccessDenied" in str(e.value)


# ---------------------------------------------------------------------------
# get_client и настройки API
# ---------------------------------------------------------------------------

def test_get_client_returns_modulbank_client(app, db):
    account = make_account(db)
    client = get_client(account)
    assert isinstance(client, ModulbankClient)
    assert client.token == "real-token"
    assert client.account_number == ACC_NUMBER
    assert client.sandbox is False


def test_get_client_none_without_token(app, db):
    account = make_account(db, token=None)
    assert get_client(account) is None


def test_get_client_sandbox_works_without_token(app, db):
    account = make_account(db, sandbox=True, token=None)
    client = get_client(account)
    assert client is not None and client.token == "sandboxtoken"


def test_save_api_settings_stores_encrypted_token_and_keeps_on_blank(app, db, client):
    account = BankAccount(bank_name="Модульбанк", checking_account=ACC_NUMBER)
    db.add(account)
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")

    url = f"/cooperative/bank-accounts/{account.id}/api-settings"
    resp = client.post(url, data={"api_provider": "modulbank", "api_token": "  tok-123  "})
    assert resp.status_code == 302
    db.expire_all()
    account = database.db_session.get(BankAccount, account.id)
    assert account.api_provider == BankApiProvider.MODULBANK
    cred = account.api_credential
    assert cred.api_token_encrypted and "tok-123" not in cred.api_token_encrypted
    assert crypto.decrypt(cred.api_token_encrypted) == "tok-123"
    assert cred.sandbox is False

    client.post(url, data={"api_provider": "modulbank", "api_token": ""})
    db.expire_all()
    cred = database.db_session.get(BankAccount, account.id).api_credential
    assert crypto.decrypt(cred.api_token_encrypted) == "tok-123"


@pytest.mark.parametrize("provider", ["vtb", "tbank"])
def test_dropped_providers_not_accepted(app, db, client, provider):
    account = BankAccount(bank_name="ВТБ", checking_account=ACC_NUMBER)
    db.add(account)
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")
    resp = client.post(f"/cooperative/bank-accounts/{account.id}/api-settings", data={"api_provider": provider})
    assert resp.status_code == 400


def test_bank_accounts_page_renders_modulbank_settings(app, db, client):
    make_account(db)
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")
    resp = client.get("/finance/bank-accounts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="api_token"' in html
    assert 'name="refresh_token"' not in html  # сберовские поля не показываются для Модульбанка
