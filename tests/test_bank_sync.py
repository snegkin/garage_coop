"""
Тесты интеграции с API банка (app/bank_api/, app/bank_sync.py).

Реальные сетевые запросы к банку не делаются (домен банка не в allowlist
сети окружения, да и смысла нет — контракт с банком не проверить без
боевого доступа): роуты синхронизации тестируются с подменённым
(monkeypatch) get_client, чтобы проверить именно логику приложения —
права доступа, дедупликацию, обработку ошибок BankApiError, разнесение
платежей из реестра. Сам SberbankClient проверяется только на разбор
ответа банка (_parse_transaction) — без сети.
"""
import datetime as dt
import os
from decimal import Decimal

import pytest

from app import database
from app import bank_sync
from app.bank_api import crypto, get_client
from app.bank_api.base import (
    BankApiError, BalanceInfo, StatementLine, ChargeRegistryResult, PaymentRegistryItem,
)
from app.bank_api.sberbank import _parse_transaction
from app.accounting import balance
from app.models import (
    RoleEnum, BankAccount, BankApiProvider, BankApiCredential, BankStatementLine,
    ChargeRegistryBatch, ChargeRegistryStatus, PaymentRegistryEntry,
    MemberAccount, FeeType, Charge, Payment, GarageContact, PersonalAccount,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login
from app.bank_api import registry_file
from app.bank_api.base import ChargeRegistryItem as _ChargeRegistryItem


def make_bank_account(db_session, provider=BankApiProvider.NONE, **kwargs):
    account = BankAccount(
        bank_name="Сбербанк", checking_account="40703810000000000001", api_provider=provider, **kwargs,
    )
    db_session.add(account)
    db_session.flush()
    return account


def make_credential(db_session, account, client_id="id1", secret="s3cret", refresh_token="r3fresh", sandbox=True, app=None):
    """`app` не используется для отдельного контекста — вызывающий тест уже
    находится внутри app_context, установленного фикстурой `db` (см.
    conftest.py). Открывать здесь ЕЩЁ один `with app.app_context()` нельзя:
    при выходе из вложенного контекста срабатывает teardown_appcontext ->
    database.db_session.remove(), которая откатывает ещё не закоммиченную
    транзакцию текущего теста (в т.ч. только что созданный BankAccount) —
    и следующая вставка падает по внешнему ключу."""
    cred = BankApiCredential(
        bank_account_id=account.id, client_id=client_id,
        client_secret_encrypted=crypto.encrypt(secret),
        refresh_token_encrypted=crypto.encrypt(refresh_token) if refresh_token else None,
        sandbox=sandbox,
    )
    db_session.add(cred)
    db_session.flush()
    return cred


# ---------------------------------------------------------------------------
# Шифрование секрета
# ---------------------------------------------------------------------------

def test_crypto_roundtrip(app):
    with app.app_context():
        token = crypto.encrypt("my-secret-value")
        assert token != "my-secret-value"
        assert crypto.decrypt(token) == "my-secret-value"


def test_crypto_empty_secret_not_encrypted(app):
    with app.app_context():
        assert crypto.encrypt("") == ""
        assert crypto.decrypt("") is None
        assert crypto.decrypt(None) is None


def test_crypto_decrypt_garbage_returns_none(app):
    with app.app_context():
        assert crypto.decrypt("not-a-valid-fernet-token") is None


# ---------------------------------------------------------------------------
# Фабрика клиента
# ---------------------------------------------------------------------------

def test_get_client_none_when_provider_is_none(app, db):
    account = make_bank_account(db, provider=BankApiProvider.NONE)
    with app.app_context():
        assert get_client(account) is None


def test_get_client_none_for_unsupported_provider(app, db):
    account = make_bank_account(db, provider=BankApiProvider.VTB)
    make_credential(db, account)
    with app.app_context():
        assert get_client(account) is None


def test_get_client_none_without_credentials(app, db):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    with app.app_context():
        assert get_client(account) is None


def test_get_client_none_without_refresh_token(app, db):
    """client_id/secret заданы, но refresh_token — нет: клиент не
    создаётся. Sber API работает не по client_credentials (только
    client_id/secret недостаточно), а по access_token/refresh_token,
    выданным конкретному пользователю СберБизнес — см. докстринг
    BankApiCredential в models.py."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account, client_id="abc", secret="s3cret", refresh_token=None)
    with app.app_context():
        assert get_client(account) is None


def test_get_client_returns_sberbank_client_when_configured(app, db):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account, client_id="abc", secret="s3cret", refresh_token="r3fresh-token")
    with app.app_context():
        client = get_client(account)
        assert client is not None
        assert client.client_id == "abc"
        assert client.refresh_token == "r3fresh-token"
        assert client.account_number == account.checking_account
        assert client.base_url == "https://fintech-test.sberbank.ru:9443"  # sandbox по умолчанию
        assert client.token_url == "https://fintech-test.sberbank.ru:9443/ic/sso/api/v2/oauth/token"


def test_get_client_uses_prod_base_url_when_not_sandbox(app, db):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account, sandbox=False)
    with app.app_context():
        client = get_client(account)
        assert client.base_url == "https://fintech.sberbank.ru:9443"


# ---------------------------------------------------------------------------
# Разбор операции выписки СберБизнес (без сети)
# ---------------------------------------------------------------------------

def test_parse_transaction_credit():
    raw = {
        "uuid": "op-1",
        "operationDate": "2026-08-20",
        "direction": "CREDIT",
        "amountRub": {"amount": "1500.00"},
        "rurTransfer": {"payerName": "Иванов И.И.", "payerInn": "123456789012"},
        "paymentPurpose": "Членский взнос",
        "number": "17",
    }
    line = _parse_transaction(raw, dt.date(2026, 8, 20))
    assert line.direction == "credit"
    assert line.amount == Decimal("1500.00")
    assert line.counterparty_name == "Иванов И.И."
    assert line.external_uid == "op-1"


# ---------------------------------------------------------------------------
# Настройки API — только председатель
# ---------------------------------------------------------------------------

def test_save_api_settings_requires_chairman(app, db, client):
    account = make_bank_account(db)
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{account.id}/api-settings",
        data={"api_provider": "sberbank", "client_id": "x", "client_secret": "y"},
    )
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.get(BankAccount, account.id).api_provider == BankApiProvider.NONE


def test_save_api_settings_chairman_stores_encrypted_secret(app, db, client):
    account = make_bank_account(db)
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{account.id}/api-settings",
        data={
            "api_provider": "sberbank", "client_id": "my-client", "client_secret": "top-secret",
            "refresh_token": "my-refresh-token", "sandbox": "on",
        },
    )
    assert resp.status_code == 302
    db.expire_all()
    account = database.db_session.get(BankAccount, account.id)
    assert account.api_provider == BankApiProvider.SBERBANK
    cred = account.api_credential
    assert cred.client_id == "my-client"
    assert cred.client_secret_encrypted != "top-secret"
    assert cred.refresh_token_encrypted != "my-refresh-token"
    with app.app_context():
        assert crypto.decrypt(cred.client_secret_encrypted) == "top-secret"
        assert crypto.decrypt(cred.refresh_token_encrypted) == "my-refresh-token"


def test_save_api_settings_blank_secret_keeps_existing(app, db, client):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account, client_id="old-id", secret="original-secret", refresh_token="original-refresh")
    make_user(db, "chair2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair2", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{account.id}/api-settings",
        data={"api_provider": "sberbank", "client_id": "old-id", "client_secret": "", "refresh_token": ""},
    )
    assert resp.status_code == 302
    db.expire_all()
    cred = database.db_session.get(BankAccount, account.id).api_credential
    with app.app_context():
        assert crypto.decrypt(cred.client_secret_encrypted) == "original-secret"
        assert crypto.decrypt(cred.refresh_token_encrypted) == "original-refresh"


# ---------------------------------------------------------------------------
# Ротация refresh_token при обновлении access_token
# ---------------------------------------------------------------------------

def test_persist_rotated_refresh_token_updates_credential(app, db):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    cred = make_credential(db, account, refresh_token="old-refresh")
    db.commit()

    class _FakeClient:
        rotated_refresh_token = "new-refresh-from-bank"

    with app.app_context():
        bank_sync._persist_rotated_refresh_token(cred, _FakeClient())
        assert crypto.decrypt(cred.refresh_token_encrypted) == "new-refresh-from-bank"


def test_persist_rotated_refresh_token_noop_when_not_rotated(app, db):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    cred = make_credential(db, account, refresh_token="stays-the-same")
    db.commit()
    original = cred.refresh_token_encrypted

    class _FakeClient:
        rotated_refresh_token = None

    bank_sync._persist_rotated_refresh_token(cred, _FakeClient())
    assert cred.refresh_token_encrypted == original


# ---------------------------------------------------------------------------
# Синхронизация баланса — с подменённым клиентом
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self, balance_result=None, balance_error=None, statement_result=None):
        self._balance_result = balance_result
        self._balance_error = balance_error
        self._statement_result = statement_result or []

    def get_balance(self):
        if self._balance_error:
            raise self._balance_error
        return self._balance_result

    def get_statement(self, date_from, date_to):
        return self._statement_result


def test_sync_balance_updates_account(app, db, client, monkeypatch):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account)
    make_user(db, "chair3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair3", "pass12345")

    stub = _StubClient(balance_result=BalanceInfo(amount=Decimal("12345.67"), as_of=dt.date(2026, 8, 24)))
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    resp = client.post(f"/cooperative/bank-accounts/{account.id}/sync-balance")
    assert resp.status_code == 302
    db.expire_all()
    updated = database.db_session.get(BankAccount, account.id)
    assert updated.balance == Decimal("12345.67")
    assert updated.balance_updated_at == dt.date(2026, 8, 24)
    assert updated.api_credential.last_error is None


def test_sync_balance_error_is_recorded(app, db, client, monkeypatch):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account)
    make_user(db, "chair4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair4", "pass12345")

    stub = _StubClient(balance_error=BankApiError("банк недоступен"))
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    resp = client.post(f"/cooperative/bank-accounts/{account.id}/sync-balance")
    assert resp.status_code == 302
    db.expire_all()
    updated = database.db_session.get(BankAccount, account.id)
    assert updated.balance is None  # не изменился
    assert "банк недоступен" in updated.api_credential.last_error


# ---------------------------------------------------------------------------
# Выписка — дедупликация по external_uid
# ---------------------------------------------------------------------------

def test_sync_statement_deduplicates_by_external_uid(app, db, client, monkeypatch):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account)
    make_user(db, "chair5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.add(BankStatementLine(
        bank_account_id=account.id, external_uid="dup-1", operation_date=dt.date(2026, 8, 1),
        direction="credit", amount=Decimal("100.00"),
    ))
    db.commit()
    login(client, "chair5", "pass12345")

    stub = _StubClient(statement_result=[
        StatementLine(external_uid="dup-1", operation_date=dt.date(2026, 8, 1), direction="credit", amount=Decimal("100.00")),
        StatementLine(external_uid="new-2", operation_date=dt.date(2026, 8, 2), direction="debit", amount=Decimal("50.00")),
    ])
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    resp = client.post(
        f"/cooperative/bank-accounts/{account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-02"},
    )
    assert resp.status_code == 302
    lines = database.db_session.query(BankStatementLine).filter_by(bank_account_id=account.id).all()
    assert len(lines) == 2  # дубль не добавился


# ---------------------------------------------------------------------------
# Распознавание номера лицевого счёта в назначении платежа и авто-погашение
# ---------------------------------------------------------------------------

def test_extract_account_number_from_real_style_purpose():
    text = "ЛС 10640; ЧЛЕНСКИЕ ВЗНОСЫ (ФАМИЛИЯ И.О.);0126;ФАМИЛИЯ ИМЯ"
    assert bank_sync.extract_account_number(text) == "10640"


def test_extract_account_number_variants():
    assert bank_sync.extract_account_number("лс10640 взнос") == "10640"  # строчными, без пробела
    assert bank_sync.extract_account_number("ЛС: 10640") == "10640"
    assert bank_sync.extract_account_number("ЛС №10640") == "10640"
    assert bank_sync.extract_account_number("Перевод между своими счетами") is None
    assert bank_sync.extract_account_number(None) is None


def test_extract_account_number_slash_and_three_letter_variants():
    """Найдено на реальной выписке (структура и форматы сохранены как есть
    в tests/fixtures/statement_sample.csv, но все ФИО/ИНН/адреса/номера
    счетов в самом файле — вымышленные, заменены при подготовке фикстуры,
    см. context.md): один и тот же банк/подключение вперемешку присылает
    «ЛС», «Л/С» (слэш между буквами) и «ЛСИ» (третья буква, иногда с
    пробелом перед двоеточием) — без «ЛСИ» регэксп ловил заметно меньше в
    реальных данных (10 из 81 против 29 из 81 с ним)."""
    assert bank_sync.extract_account_number("Л/С:20700;ПРД:01.2026") == "20700"
    assert bank_sync.extract_account_number("QR5/Л/С 10550/Период опл 0126") == "10550"
    assert bank_sync.extract_account_number("ЧЛЕНСКИЕ ВЗНОСЫ Л/С 10760") == "10760"
    assert bank_sync.extract_account_number("ЛСИ:20780;ПРД:0126") == "20780"
    assert bank_sync.extract_account_number("ЛСИ :20780;ПРД:0126") == "20780"  # пробел перед двоеточием
    # Полные строки назначения платежа (не только сам фрагмент с меткой) —
    # чтобы наличие «(ФАМИЛИЯ И.О.)» и «;ПРД:...» после номера не мешало
    # найти сам номер счёта в начале/середине строки.
    assert bank_sync.extract_account_number("ЧЛЕНСКИЕ ВЗНОСЫ (ЕРШОВА Н.Ю.);ЛСИ:10180;ПРД:0126") == "10180"
    assert bank_sync.extract_account_number("ЗЕМЕЛЬНЫЙ НАЛОГ (ЕРШОВА Н.Ю.);ЛСИ:20180;ПРД:0126") == "20180"


def test_is_aggregate_registry_payment():
    aggregate = (
        "ПО ПРИНЯТЫМ ПЛАТЕЖАМ С 12/02/2026 ПО 12/02/2026 НА ОБЩУЮ СУММУ 5120.00,"
        "В Т.Ч.УСЛ.БАНКА:0.00,В КОЛ-ВЕ 4,СОГЛАСНО ЭЛ.РЕЕСТРУ EPS..._051.txt"
    )
    assert bank_sync.is_aggregate_registry_payment(aggregate) is True
    assert bank_sync.is_aggregate_registry_payment("ЛС 10640; ЧЛЕНСКИЕ ВЗНОСЫ") is False
    assert bank_sync.is_aggregate_registry_payment(None) is False


def test_account_number_extraction_against_statement_sample():
    """Регрессия на образце реального экспорта выписки (ФИО/ИНН/адреса и
    номера счетов в файле — вымышленные, структура и форматы записи —
    подлинные, см. docstring выше) — доля зачислений, для которых
    извлекается номер лицевого счёта из текста, не должна упасть ниже уже
    достигнутой (29 из 81) при будущих правках регэкспа."""
    import csv
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "statement_sample.csv")
    with open(fixture_path, encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter=";"))
    credits = [r for r in rows if len(r) >= 8 and r[1] == "зачисление"]
    assert len(credits) == 81  # сам фикстур не должен незаметно измениться

    with_number = sum(1 for r in credits if bank_sync.extract_account_number(r[5]))
    aggregate = sum(1 for r in credits if bank_sync.is_aggregate_registry_payment(r[5]))
    assert with_number >= 29
    assert aggregate == 22  # агрегированные проводки по реестру — не пытаемся разносить как обычное зачисление


def test_sync_statement_auto_allocates_credit_with_account_number(app, db, client, monkeypatch):
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10640",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("1710.00")))

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, bank_account)
    make_user(db, "chair13", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair13", "pass12345")

    stub = _StubClient(statement_result=[
        StatementLine(
            external_uid="op-1", operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1710.00"),
            payment_purpose="ЛС 10640; ЧЛЕНСКИЕ ВЗНОСЫ (ФАМИЛИЯ И.О.);0126;ФАМИЛИЯ ИМЯ",
        ),
    ])
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert resp.status_code == 302

    line = database.db_session.query(BankStatementLine).filter_by(bank_account_id=bank_account.id).one()
    assert line.account_number == "10640"
    assert line.matched_payment_id is not None
    assert balance(member_account) == Decimal("0.00")  # долг погашен автоматически, FIFO уже применён


def test_sync_statement_does_not_auto_allocate_debits(app, db, client, monkeypatch):
    """Списания не должны пытаться погасить чей-то долг, даже если в тексте
    случайно нашёлся похожий на ЛС номер."""
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, bank_account)
    make_user(db, "chair14", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair14", "pass12345")

    stub = _StubClient(statement_result=[
        StatementLine(
            external_uid="op-2", operation_date=dt.date(2026, 8, 15), direction="debit", amount=Decimal("500.00"),
            payment_purpose="ЛС 10640; оплата подрядчику",
        ),
    ])
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    line = database.db_session.query(BankStatementLine).filter_by(bank_account_id=bank_account.id).one()
    assert line.account_number == "10640"  # распознан
    assert line.matched_payment_id is None  # но не разнесён — это списание


def test_sync_statement_leaves_unmatched_account_number_unallocated(app, db, client, monkeypatch):
    """Номер распознан, но такого лицевого счёта в системе нет — строка
    остаётся неразнесённой, не создаёт платёж «в никуда»."""
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, bank_account)
    make_user(db, "chair15", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair15", "pass12345")

    stub = _StubClient(statement_result=[
        StatementLine(
            external_uid="op-3", operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1000.00"),
            payment_purpose="ЛС 99999; ЧЛЕНСКИЕ ВЗНОСЫ",
        ),
    ])
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    line = database.db_session.query(BankStatementLine).filter_by(bank_account_id=bank_account.id).one()
    assert line.account_number == "99999"
    assert line.matched_payment_id is None


def test_allocate_statement_line_manually(app, db, client):
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="60077",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("500.00")))

    bank_account = make_bank_account(db)
    line = BankStatementLine(
        bank_account_id=bank_account.id, external_uid="op-manual", operation_date=dt.date(2026, 8, 20),
        direction="credit", amount=Decimal("500.00"), payment_purpose="Взнос без номера ЛС в тексте",
    )
    db.add(line)
    make_user(db, "chair16", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair16", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/statement/{line.id}/allocate",
        data={"account_number": "60077"},
    )
    assert resp.status_code == 302
    db.expire_all()
    updated_line = database.db_session.get(BankStatementLine, line.id)
    assert updated_line.account_number == "60077"
    assert updated_line.matched_payment_id is not None
    assert balance(member_account) == Decimal("0.00")


def test_allocate_statement_line_empty_field_falls_back_to_purpose_text(app, db, client):
    """Регрессия: номер счёта явно указан в тексте назначения платежа
    («ЛСИ:10180»), но line.account_number в БД пуст (например, строка
    синхронизировалась до того, как этот вариант метки стали распознавать)
    — нажатие «Разнести» с ПУСТЫМ полем ввода должно само найти номер в
    тексте, а не требовать ручного ввода того, что и так есть в назначении
    платежа. Раньше здесь был баг: подсказка «Предполагаемый счёт» (см.
    статью statement()) вызывает extract_account_number и находит номер,
    а сама кнопка «Разнести» — нет, из-за чего с виду «подсказанный»
    платёж не разносился."""
    person = make_person(db, full_name="Ершова Надежда Юрьевна")
    garage = make_garage(db, number="18")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10180",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("1710.00")))

    bank_account = make_bank_account(db)
    line = BankStatementLine(
        bank_account_id=bank_account.id, external_uid="op-ershova", operation_date=dt.date(2026, 8, 20),
        direction="credit", amount=Decimal("1710.00"),
        payment_purpose="ЧЛЕНСКИЕ ВЗНОСЫ (ЕРШОВА Н.Ю.);ЛСИ:10180;ПРД:0126",
        counterparty_name="Ершова Надежда Юрьевна",
        account_number=None,  # намеренно пусто — как в реальном баг-репорте
    )
    db.add(line)
    make_user(db, "chair19", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair19", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/statement/{line.id}/allocate",
        data={"account_number": ""},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["account_number"] == "10180"
    db.expire_all()
    updated_line = database.db_session.get(BankStatementLine, line.id)
    assert updated_line.matched_payment_id is not None
    assert balance(member_account) == Decimal("0.00")


def test_allocate_statement_line_override_wrong_account_does_not_fall_back_to_name(app, db, client):
    """Явно введённый в поле номер счёта, если такого счёта нет, не должен
    приводить к тихому разнесению по имени плательщика — председатель мог
    просто опечататься в номере, а не иметь в виду какой-то другой счёт."""
    person = make_person(db, full_name="Сидорова Анна Викторовна")
    garage = make_garage(db, number="22")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="30099",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("250.00")))

    bank_account = make_bank_account(db)
    line = BankStatementLine(
        bank_account_id=bank_account.id, external_uid="op-typo", operation_date=dt.date(2026, 8, 20),
        direction="credit", amount=Decimal("250.00"), payment_purpose="Взнос",
        counterparty_name="Сидорова Анна Викторовна",  # по имени нашлось бы однозначно
    )
    db.add(line)
    make_user(db, "chair20", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair20", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/statement/{line.id}/allocate",
        data={"account_number": "99999"},  # опечатка/несуществующий номер
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is False
    assert "99999" in data["message"]
    db.expire_all()
    updated_line = database.db_session.get(BankStatementLine, line.id)
    assert updated_line.matched_payment_id is None
    assert balance(member_account) == Decimal("-250.00")  # не тронут


def test_allocate_statement_line_rejects_debit(app, db, client):
    bank_account = make_bank_account(db)
    line = BankStatementLine(
        bank_account_id=bank_account.id, external_uid="op-debit", operation_date=dt.date(2026, 8, 20),
        direction="debit", amount=Decimal("500.00"),
    )
    db.add(line)
    make_user(db, "chair17", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair17", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/statement/{line.id}/allocate",
        data={"account_number": "60077"},
    )
    assert resp.status_code == 302
    db.expire_all()
    updated_line = database.db_session.get(BankStatementLine, line.id)
    assert updated_line.matched_payment_id is None


# ---------------------------------------------------------------------------
# Поиск лицевого счёта по ФИО / лицам для связи — app/bank_sync.py
# ---------------------------------------------------------------------------

def test_initials_key():
    assert bank_sync._initials_key("Иванов Иван Иванович") == "ИВАНОВ И.И."
    assert bank_sync._initials_key("Иванов Иван") == "ИВАНОВ И."
    assert bank_sync._initials_key("Иванов") is None  # нечего сокращать


def test_extract_initials_candidates():
    text = "ЧЛЕНСКИЕ ВЗНОСЫ (ПРИМЕРНЫЙ Г.Н.);0126;ФАМИЛИЯ ИМЯ"
    assert bank_sync._extract_initials_candidates(text) == {"ПРИМЕРНЫЙ Г.Н."}
    assert bank_sync._extract_initials_candidates("обычный текст без имён") == set()
    assert bank_sync._extract_initials_candidates(None) == set()


def test_find_persons_by_name_full_name_match(db):
    person = make_person(db, full_name="Иванов Иван Иванович")
    db.commit()
    found = bank_sync._find_persons_by_name("Иванов Иван Иванович", None)
    assert person in found


def test_find_persons_by_name_initials_in_purpose(db):
    """Ключевой сценарий из примера банка: плательщик — один человек, а в
    назначении платежа в скобках указан ДРУГОЙ, за кого платят."""
    payer = make_person(db, full_name="Примерная Ольга Викторовна")
    owner = make_person(db, full_name="Примерный Геннадий Николаевич")
    db.commit()
    found = bank_sync._find_persons_by_name(
        payer_name="Примерная Ольга Викторовна",
        purpose="ЧЛЕНСКИЕ ВЗНОСЫ (Примерный Г.Н.)",
    )
    assert payer in found
    assert owner in found


def test_accounts_for_person_via_ownership_and_contact(db):
    owner = make_person(db, full_name="Собственников Олег Олегович")
    contact = make_person(db, full_name="Доверенный Пётр Петрович")
    garage = make_garage(db, number="55")
    make_ownership(db, garage, owner)
    db.add(GarageContact(garage_id=garage.id, person_id=contact.id, relation="доверенное лицо"))
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=owner.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="70088",
    )
    db.add(member_account)
    db.commit()

    owner_accounts = bank_sync._accounts_for_person(owner)
    assert ("member", member_account) in [(k, o) for k, o in owner_accounts if k == "member" and o.id == member_account.id]

    # Лицо для связи не имеет собственного счёта, но через него поднимается счёт владельца.
    contact_accounts = bank_sync._accounts_for_person(contact)
    assert any(k == "member" and o.id == member_account.id for k, o in contact_accounts)


def test_resolve_account_by_name_unambiguous(db):
    person = make_person(db, full_name="Уникальнов Уникал Уникалович")
    garage = make_garage(db, number="66")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="80099",
    )
    db.add(member_account)
    db.commit()

    kind, target, account_number = bank_sync._resolve_account_by_name("Уникальнов Уникал Уникалович", None)
    assert kind == "member"
    assert account_number == "80099"


def test_resolve_account_by_name_ambiguous_without_fee_type_hint_stays_unresolved(db):
    """У человека два счёта (два вида взноса на один гараж) — без указания
    вида взноса в тексте нельзя однозначно выбрать, разносить не должны."""
    person = make_person(db, full_name="Двухсчётов Два Двоевич")
    garage = make_garage(db, number="77")
    make_ownership(db, garage, person)
    dues = FeeType(code="10", name="Членский взнос")
    tax = FeeType(code="20", name="Земельный налог")
    db.add_all([dues, tax])
    db.flush()
    db.add(MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=dues.id, account_number="90011"))
    db.add(MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=tax.id, account_number="90012"))
    db.commit()

    kind, target, account_number = bank_sync._resolve_account_by_name("Двухсчётов Два Двоевич", None)
    assert kind is None  # неоднозначно — не разносим наугад


def test_resolve_account_by_name_narrowed_by_fee_type(db):
    """Тот же случай, но в назначении платежа назван конкретный вид
    взноса — сужение по FeeType.name разрешает неоднозначность."""
    person = make_person(db, full_name="Трёхсчётов Три Троевич")
    garage = make_garage(db, number="88")
    make_ownership(db, garage, person)
    dues = FeeType(code="10", name="Членский взнос")
    tax = FeeType(code="20", name="Земельный налог")
    db.add_all([dues, tax])
    db.flush()
    db.add(MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=dues.id, account_number="90021"))
    tax_account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=tax.id, account_number="90022")
    db.add(tax_account)
    db.commit()

    kind, target, account_number = bank_sync._resolve_account_by_name(
        "Трёхсчётов Три Троевич", "ЗЕМЕЛЬНЫЙ НАЛОГ (Трёхсчётов Т.Т.)",
    )
    assert kind == "member"
    assert account_number == "90022"


def test_sync_statement_auto_allocates_by_name_when_no_account_number(app, db, client, monkeypatch):
    """Полный e2e-сценарий, аналогичный примерам банка: в выписке нет «ЛС»,
    только ФИО плательщика и/или ФИО в назначении платежа."""
    person = make_person(db, full_name="Безномеров Иван Иванович")
    garage = make_garage(db, number="99")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10555",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("1710.00")))

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, bank_account)
    make_user(db, "chair18", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair18", "pass12345")

    stub = _StubClient(statement_result=[
        StatementLine(
            external_uid="op-name-1", operation_date=dt.date(2026, 8, 15), direction="credit",
            amount=Decimal("1710.00"), counterparty_name="Безномеров Иван Иванович",
            payment_purpose="ЧЛЕНСКИЕ ВЗНОСЫ",
        ),
    ])
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert resp.status_code == 302

    line = database.db_session.query(BankStatementLine).filter_by(bank_account_id=bank_account.id).one()
    assert line.account_number == "10555"  # проставлен, хоть и не было в исходном тексте
    assert line.matched_payment_id is not None
    assert balance(member_account) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Реестр начислений — собирает должников
# ---------------------------------------------------------------------------

def test_debtor_items_includes_negative_balance_member_account(app, db):
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10099")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("300.00")))
    db.commit()
    assert balance(account) == Decimal("-300.00")

    with app.app_context():
        items = bank_sync._debtor_items()
    matching = [i for i in items if i.account_number == "10099"]
    assert len(matching) == 1
    assert matching[0].amount == Decimal("300.00")


# ---------------------------------------------------------------------------
# Реестр платежей — разнесение записи на найденный лицевой счёт
# ---------------------------------------------------------------------------

def test_allocate_payment_registry_entry_creates_payment(app, db, client):
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="20055",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("400.00")))
    db.flush()

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    entry = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="ext-1", account_number="20055",
        payer_name=person.full_name, amount=Decimal("400.00"), operation_date=dt.date(2026, 8, 10),
    )
    db.add(entry)
    make_user(db, "chair6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair6", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/{entry.id}/allocate"
    )
    assert resp.status_code == 302
    db.expire_all()
    updated_entry = database.db_session.get(PaymentRegistryEntry, entry.id)
    assert updated_entry.matched_payment_id is not None
    payment = database.db_session.get(Payment, updated_entry.matched_payment_id)
    assert payment.amount == Decimal("400.00")
    assert payment.account_id == member_account.id
    assert balance(member_account) == Decimal("0.00")  # разнесено через reallocate_member_charges


def test_allocate_payment_registry_entry_manual_override(app, db, client):
    """Ручное поле ввода номера счёта (см. payment_registry.html) — то же
    переопределение, что и на странице выписки: если в записи реестра
    сохранён неверный/отсутствующий номер, председатель может ввести
    правильный при разнесении."""
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="40011",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("200.00")))

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    entry = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="ext-override", account_number=None,
        amount=Decimal("200.00"), operation_date=dt.date(2026, 8, 10),
    )
    db.add(entry)
    make_user(db, "chair21", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair21", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/{entry.id}/allocate",
        data={"account_number": "40011"},
    )
    assert resp.status_code == 302
    db.expire_all()
    updated_entry = database.db_session.get(PaymentRegistryEntry, entry.id)
    assert updated_entry.account_number == "40011"
    assert updated_entry.matched_payment_id is not None
    assert balance(member_account) == Decimal("0.00")


def test_allocate_payment_registry_entry_override_wrong_account_does_not_fall_back_to_name(app, db, client):
    """Тот же принцип, что и для выписки (см.
    test_allocate_statement_line_override_wrong_account_does_not_fall_back_to_name):
    явно введённый неверный номер счёта — ошибка ввода, а не повод тихо
    разнести платёж по имени плательщика на какой-то другой счёт."""
    person = make_person(db, full_name="Кузнецова Ольга Сергеевна")
    garage = make_garage(db, number="23")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="40022",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("150.00")))

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    entry = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="ext-typo", account_number=None,
        payer_name="Кузнецова Ольга Сергеевна",  # по имени нашлось бы однозначно
        amount=Decimal("150.00"), operation_date=dt.date(2026, 8, 10),
    )
    db.add(entry)
    make_user(db, "chair22", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair22", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/{entry.id}/allocate",
        data={"account_number": "99999"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is False
    assert "99999" in data["message"]
    db.expire_all()
    updated_entry = database.db_session.get(PaymentRegistryEntry, entry.id)
    assert updated_entry.matched_payment_id is None
    assert balance(member_account) == Decimal("-150.00")


def test_allocate_payment_registry_entry_unknown_account_number(app, db, client):
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    entry = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="ext-2", account_number="does-not-exist",
        amount=Decimal("100.00"), operation_date=dt.date(2026, 8, 10),
    )
    db.add(entry)
    make_user(db, "chair7", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair7", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/{entry.id}/allocate"
    )
    assert resp.status_code == 302
    db.expire_all()
    updated_entry = database.db_session.get(PaymentRegistryEntry, entry.id)
    assert updated_entry.matched_payment_id is None


def test_allocate_all_payment_registry_entries(app, db, client):
    person_a = make_person(db, full_name="Иванов Иван Иванович")
    person_b = make_person(db, full_name="Петров Пётр Петрович")
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account_a = MemberAccount(
        person_id=person_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="20055",
    )
    account_b = MemberAccount(
        person_id=person_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="20056",
    )
    db.add_all([account_a, account_b])
    db.flush()
    db.add(Charge(account_id=account_a.id, year=2026, amount=Decimal("400.00")))
    db.add(Charge(account_id=account_b.id, year=2026, amount=Decimal("300.00")))
    db.flush()

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    entry_a = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="bulk-1", account_number="20055",
        payer_name=person_a.full_name, amount=Decimal("400.00"), operation_date=dt.date(2026, 8, 10),
    )
    entry_b = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="bulk-2", account_number="20056",
        payer_name=person_b.full_name, amount=Decimal("300.00"), operation_date=dt.date(2026, 8, 11),
    )
    entry_unknown = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="bulk-3", account_number="does-not-exist",
        amount=Decimal("100.00"), operation_date=dt.date(2026, 8, 12),
    )
    db.add_all([entry_a, entry_b, entry_unknown])
    make_user(db, "chair8", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair8", "pass12345")

    resp = client.post(f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/allocate-all")
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(PaymentRegistryEntry, entry_a.id).matched_payment_id is not None
    assert database.db_session.get(PaymentRegistryEntry, entry_b.id).matched_payment_id is not None
    assert database.db_session.get(PaymentRegistryEntry, entry_unknown.id).matched_payment_id is None
    assert balance(account_a) == Decimal("0.00")
    assert balance(account_b) == Decimal("0.00")


def test_allocate_all_payment_registry_entries_respects_date_filter(app, db, client):
    """Кнопка передаёт date_from/date_to текущего фильтра страницы (см.
    payment_registry.html) — массовое разнесение должно ограничиваться
    только показанными записями, а не молча трогать остальные."""
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="20055",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("700.00")))
    db.flush()

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    entry_in_window = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="win-1", account_number="20055",
        payer_name=person.full_name, amount=Decimal("400.00"), operation_date=dt.date(2026, 8, 10),
    )
    entry_out_of_window = PaymentRegistryEntry(
        bank_account_id=bank_account.id, external_id="win-2", account_number="20055",
        payer_name=person.full_name, amount=Decimal("300.00"), operation_date=dt.date(2026, 1, 1),
    )
    db.add_all([entry_in_window, entry_out_of_window])
    make_user(db, "chair9", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair9", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/allocate-all",
        data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert resp.status_code == 302
    db.expire_all()

    assert database.db_session.get(PaymentRegistryEntry, entry_in_window.id).matched_payment_id is not None
    assert database.db_session.get(PaymentRegistryEntry, entry_out_of_window.id).matched_payment_id is None


def test_charge_registry_batch_status_choices():
    """Проверка того, что модель статусов реестра не разошлась с шаблоном
    (charge_registry.html: status_classes ожидает конкретные значения)."""
    values = {s.value for s in ChargeRegistryStatus}
    assert values == {"draft", "sent", "accepted", "rejected", "error"}


# ---------------------------------------------------------------------------
# Файловый формат реестров (CP1251) — см. app/bank_api/registry_file.py
# Формат по умолчанию — РЕАЛЬНЫЙ формат СберБизнес Онлайн, полученный от
# Sne как образцы файлов (tests/fixtures/sample_charge_registry.txt,
# sample_payment_registry.txt) — не придуман, тесты сверяются с ними
# напрямую, байт в байт, а не только с синтетическими данными.
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def test_build_charge_registry_file_matches_real_sample():
    """Один в один воспроизводит первую строку реального образца файла
    реестра начислений — формат по умолчанию не выдуман, это то, что
    реально принимает банк (см. registry_file.DEFAULT_FORMAT)."""
    expected = open(os.path.join(FIXTURES_DIR, "sample_charge_registry.txt"), "rb").read().decode("cp1251")
    item = _ChargeRegistryItem(
        account_number="10010", payer_name="ПРИМЕРНЫЙ ГЕННАДИЙ БАТЬКОВИЧ",
        amount=Decimal("1710.00"), purpose="ЧЛЕНСКИЕ ВЗНОСЫ (ПРИМЕРНЫЙ Г.Н.)", service_code="0625",
    )
    built = registry_file.build_charge_registry_file([item]).decode("cp1251")
    assert built == expected.splitlines()[0] + "\r\n"


def test_build_charge_registry_file_is_cp1251_encoded():
    items = [_ChargeRegistryItem(
        account_number="10099", payer_name="Иванов Иван Иванович",
        amount=Decimal("1234.50"), purpose="Взнос за август",
    )]
    content = registry_file.build_charge_registry_file(items)
    assert isinstance(content, bytes)
    # UTF-8 декодирование кириллицы из CP1251 даёт мусор/ошибку — явная
    # проверка того, что файл именно CP1251, а не UTF-8.
    with pytest.raises(UnicodeDecodeError):
        content.decode("utf-8")
    text = content.decode("cp1251")
    assert "Иванов Иван Иванович" in text
    assert "10099" in text
    assert "1234.50" in text  # разделитель дробной части по умолчанию для реестра начислений — точка


def test_charge_registry_file_escapes_delimiter_in_fields():
    items = [_ChargeRegistryItem(
        account_number="10099", payer_name="Иванов; Иван",  # «;» внутри поля не должен разъехать колонки
        amount=Decimal("100.00"), purpose="Взнос",
    )]
    content = registry_file.build_charge_registry_file(items)
    line = content.decode("cp1251").splitlines()[0]
    assert line.count(";") == 4  # ровно 4 разделителя между 5 полями формата по умолчанию — «;» внутри ФИО заменён


def test_parse_payment_registry_file_matches_real_sample():
    """Реальный образец файла реестра платежей — 4 строки данных + 1 строка
    сводки (с префиксом «=»), которая должна быть пропущена, а не стать
    битой записью."""
    raw = open(os.path.join(FIXTURES_DIR, "sample_payment_registry.txt"), "rb").read()
    items = registry_file.parse_payment_registry_file(raw)
    assert len(items) == 4

    first = items[0]
    assert first.external_id == "952942116931"  # номер операции — используется как external_id
    assert first.account_number == "20470"
    assert first.payer_name == "ПРИМЕРНАЯ АННА БАТЬКОВНА"
    assert first.amount == Decimal("850.00")  # сумма начисления — то, чем гасится долг
    assert first.credited_amount == Decimal("836.40")  # реально зачислено (за вычетом комиссии)
    assert first.fee_amount == Decimal("13.60")  # комиссия банка
    assert first.operation_date == dt.date(2026, 5, 15)
    assert first.payment_purpose == "ЗЕМЕЛЬНЫЙ НАЛОГ (ПРИМЕРНАЯ А.А.)"

    # Итоговая строка "=4;5120,00;5038,08;81,92;945839;18-05-2026" не должна
    # была превратиться в пятую запись реестра.
    assert all(i.external_id != "945839" for i in items)


def test_parse_payment_registry_file_roundtrip():
    columns = ["account_number", "payer_name", "charged_amount", "date", "purpose", "operation_id"]
    fmt = registry_file.RegistryFormat(
        charge_columns=registry_file.DEFAULT_CHARGE_COLUMNS, payment_columns=columns,
        payment_decimal_separator=",", trailer_prefix=None,
    )
    raw = "10099;Иванов Иван Иванович;1234,50;20.08.2026;Взнос за август;ext-1\r\n"
    content = raw.encode("cp1251")
    items = registry_file.parse_payment_registry_file(content, fmt)
    assert len(items) == 1
    item = items[0]
    assert item.account_number == "10099"
    assert item.payer_name == "Иванов Иван Иванович"
    assert item.amount == Decimal("1234.50")
    assert item.operation_date == dt.date(2026, 8, 20)
    assert item.external_id == "ext-1"


def test_parse_payment_registry_file_skips_malformed_lines():
    raw = open(os.path.join(FIXTURES_DIR, "sample_payment_registry.txt"), "rb").read().decode("cp1251")
    raw += "garbage;too;few;fields\r\n"  # строка короче формата — должна быть пропущена, не сломать разбор
    items = registry_file.parse_payment_registry_file(raw.encode("cp1251"))
    assert len(items) == 4  # столько же, сколько в исходном образце — битая строка не добавила пятую запись


def test_registry_format_is_configurable():
    """Смена порядка/состава полей и разделителей — RegistryFormat, а не
    жёстко зашитый формат. Проверка на синтетическом формате, отличном от
    умолчания (обратный порядок части полей, «;» -> «|», запятая -> точка)."""
    custom = registry_file.RegistryFormat(
        charge_columns=["amount", "account_number", "payer_name", "purpose"],
        payment_columns=registry_file.DEFAULT_PAYMENT_COLUMNS,
        charge_decimal_separator=",", delimiter="|",
    )
    item = _ChargeRegistryItem(account_number="777", payer_name="Тест Тестович", amount=Decimal("99.90"), purpose="Взнос")
    built = registry_file.build_charge_registry_file([item], custom).decode("cp1251").strip()
    assert built == "99,90|777|Тест Тестович|Взнос"


# ---------------------------------------------------------------------------
# Скачивание/загрузка файла реестра через роуты (без обращения к банку)
# ---------------------------------------------------------------------------

def test_download_charge_registry_file_is_cp1251(app, db, client):
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="30011",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("500.00")))
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair8", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "chair8", "pass12345")

    resp = client.get(f"/cooperative/bank-accounts/{bank_account.id}/registry/charges/download")
    assert resp.status_code == 200
    assert "windows-1251" in resp.headers["Content-Type"].lower()
    text = resp.data.decode("cp1251")
    assert "30011" in text
    assert "0625" in text  # код услуги/периода по умолчанию


def test_upload_payment_registry_file_allocates_via_upsert(app, db, client):
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="40022",
    )
    db.add(member_account)
    db.flush()
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair9", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair9", "pass12345")

    raw = "15-08-2026;10-00-00;0001;0001111V;100200300;40022;Петров Пётр;Взнос;0625;250,00;245,00;5,00;5\r\n"
    from io import BytesIO
    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/upload",
        data={"registry_file": (BytesIO(raw.encode("cp1251")), "registry.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    entries = database.db_session.query(PaymentRegistryEntry).filter_by(bank_account_id=bank_account.id).all()
    assert len(entries) == 1
    assert entries[0].account_number == "40022"
    assert entries[0].amount == Decimal("250.00")
    assert entries[0].credited_amount == Decimal("245.00")
    assert entries[0].fee_amount == Decimal("5.00")

    # Повторная загрузка того же файла не создаёт дубль (та же дедупликация,
    # что и у автоматического fetch, см. _store_payment_registry_items).
    client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/upload",
        data={"registry_file": (BytesIO(raw.encode("cp1251")), "registry.txt")},
        content_type="multipart/form-data",
    )
    entries_after = database.db_session.query(PaymentRegistryEntry).filter_by(bank_account_id=bank_account.id).all()
    assert len(entries_after) == 1


def test_save_and_use_custom_charge_registry_format(app, db, client):
    """Настройка формата через роут действительно применяется при скачивании
    файла — не только в юнит-тестах RegistryFormat напрямую."""
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="50033",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("300.00")))
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair12", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair12", "pass12345")

    # Только account_number, payer_name, purpose, amount, разделитель "|", запятая как дробный разделитель.
    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/format/charges",
        data={
            "col_account_number": "1", "pos_account_number": "1",
            "col_payer_name": "1", "pos_payer_name": "2",
            "col_purpose": "1", "pos_purpose": "3",
            "col_amount": "1", "pos_amount": "4",
            "charge_decimal_separator": ",", "delimiter": "|", "encoding": "cp1251", "service_code": "9999",
        },
    )
    assert resp.status_code == 302

    resp = client.get(f"/cooperative/bank-accounts/{bank_account.id}/registry/charges/download")
    text = resp.data.decode("cp1251")
    assert "|" in text
    assert "300,00" in text  # запятая, как настроено
    assert "50033" in text



# ---------------------------------------------------------------------------
# Клиентский mTLS-сертификат — загрузка .pfx/.p12 и конвертация в PEM
# ---------------------------------------------------------------------------

def _make_test_pkcs12(passphrase: bytes | None) -> bytes:
    """Самоподписанный сертификат + ключ, упакованные в PKCS#12 — точно так
    же, как это делает личный кабинет Sber API, только с тестовым CN."""
    import datetime as _dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives.serialization import pkcs12 as pkcs12_mod

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-coop-client")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow())
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    encryption = (
        serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
    )
    return pkcs12_mod.serialize_key_and_certificates(
        name=b"test", key=key, cert=cert, cas=None, encryption_algorithm=encryption,
    )


def test_upload_client_certificate_converts_pkcs12_to_pem(app, db, client):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair10", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair10", "pass12345")

    from io import BytesIO
    p12_bytes = _make_test_pkcs12(b"my-passphrase")
    resp = client.post(
        f"/cooperative/bank-accounts/{account.id}/api-settings",
        data={
            "api_provider": "sberbank", "client_id": "cid", "client_secret": "csecret",
            "tls_cert_p12": (BytesIO(p12_bytes), "client.pfx"),
            "tls_cert_passphrase": "my-passphrase",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    db.expire_all()
    cred = database.db_session.get(BankAccount, account.id).api_credential
    assert cred.tls_cert_filename and cred.tls_key_filename

    with app.app_context():
        certs_dir = app.config["BANK_CERTS_FOLDER"]
    cert_path = os.path.join(certs_dir, cred.tls_cert_filename)
    key_path = os.path.join(certs_dir, cred.tls_key_filename)
    assert os.path.exists(cert_path)
    assert os.path.exists(key_path)
    assert open(cert_path, "rb").read().startswith(b"-----BEGIN CERTIFICATE-----")
    assert open(key_path, "rb").read().startswith(b"-----BEGIN")
    # Приватный ключ — не зашифрован в PEM (пароль использован только один
    # раз для конвертации и нигде не сохраняется) и доступен только владельцу.
    mode = oct(os.stat(key_path).st_mode)[-3:]
    assert mode == "600"


# ---------------------------------------------------------------------------
# Сопоставление реестра и выписки
# ---------------------------------------------------------------------------

def test_match_registry_and_statement_direct_by_external_id(app, db):
    """Прямое совпадение по внешнему ID банка: PaymentRegistryEntry.external_id
    == BankStatementLine.external_uid."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="ext-123",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1000.00"),
        counterparty_name="Иванов Иван Иванович",
        payment_purpose="Членский взнос",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="ext-123",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("1000.00"),
        payer_name="Иванов Иван Иванович",
        account_number="10001",
        payment_purpose="Членский взнос",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 1
    assert parametric == 0
    db.expire_all()
    assert line.matched_registry_id == entry.id
    assert entry.matched_statement_id == line.id


def test_match_registry_and_statement_parametric_by_account_number(app, db):
    """Параметрическое совпадение: разные внешние ID, но совпадают
    account_number, amount и дата (±1 день)."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-001",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1500.00"),
        account_number="20002",
        payment_purpose="Земельный налог",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-001",
        operation_date=dt.date(2026, 8, 16), amount=Decimal("1500.00"),
        payer_name="Петров Пётр Петрович",
        account_number="20002",
        payment_purpose="Земельный налог",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 1
    db.expire_all()
    assert line.matched_registry_id == entry.id
    assert entry.matched_statement_id == line.id


def test_match_registry_and_statement_no_match_different_account_numbers(app, db):
    """Нет совпадения: разные номера лицевых счетов — не сопоставлять."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-002",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1500.00"),
        account_number="20002",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-002",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("1500.00"),
        account_number="20003",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 0
    db.expire_all()
    assert line.matched_registry_id is None
    assert entry.matched_statement_id is None


def test_match_registry_and_statement_no_match_different_amounts(app, db):
    """Нет совпадения: разные суммы — не сопоставлять, даже если ЛС и дата совпадают."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-003",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1500.00"),
        account_number="20004",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-003",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("1500.50"),
        account_number="20004",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 0


def test_match_registry_and_statement_no_match_date_too_far(app, db):
    """Нет совпадения: дата отличается более чем на 1 день."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-004",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1500.00"),
        account_number="20005",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-004",
        operation_date=dt.date(2026, 8, 20), amount=Decimal("1500.00"),
        account_number="20005",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 0


def test_match_registry_and_statement_already_matched_not_rematched(app, db):
    """Уже сопоставленные записи не пересоздаются — match 1:1."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-005",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("1500.00"),
        account_number="20006",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-005",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("1500.00"),
        account_number="20006",
    )
    db.add(entry)
    db.commit()

    # Первое сопоставление
    bank_sync._match_registry_and_statement(account.id)
    db.commit()

    # Второе — должно вернуть 0, ничего не изменив
    direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 0


def test_match_registry_and_statement_multiple_pairs(app, db):
    """Несколько пар — каждая сопоставляется независимо."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    lines = [
        BankStatementLine(
            bank_account_id=account.id, external_uid=f"stmt-{i}",
            operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal(str(100 * (i + 1))),
            account_number=str(30000 + i),
        )
        for i in range(3)
    ]
    entries = [
        PaymentRegistryEntry(
            bank_account_id=account.id, external_id=f"reg-{i}",
            operation_date=dt.date(2026, 8, 15), amount=Decimal(str(100 * (i + 1))),
            account_number=str(30000 + i),
        )
        for i in range(3)
    ]
    for l in lines:
        db.add(l)
    for e in entries:
        db.add(e)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 3
    db.expire_all()
    for i in range(3):
        assert lines[i].matched_registry_id == entries[i].id
        assert entries[i].matched_statement_id == lines[i].id


def test_match_registry_and_statement_ignores_already_allocated_lines(app, db):
    """Строки выписки, уже сопоставленные с реестром (matched_registry_id != None),
    не участвуют в повторном сопоставлении."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line1 = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-10",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number="40001",
    )
    db.add(line1)

    entry1 = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-10",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("100.00"),
        account_number="40001",
    )
    db.add(entry1)

    # Уже сопоставлены вручную (нужен flush, чтобы IDs были присвоены)
    db.flush()
    line1.matched_registry_id = entry1.id
    entry1.matched_statement_id = line1.id

    # Ещё одна пара — должна сопоставиться
    line2 = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-11",
        operation_date=dt.date(2026, 8, 16), direction="credit", amount=Decimal("200.00"),
        account_number="40002",
    )
    db.add(line2)

    entry2 = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-11",
        operation_date=dt.date(2026, 8, 16), amount=Decimal("200.00"),
        account_number="40002",
    )
    db.add(entry2)
    db.commit()

    direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 1
    # Функция не делает commit — фиксируем изменения
    db.commit()
    # Функция работает с объектами из своего запроса — нужно обновить объекты теста
    db.expire_all()
    db.refresh(line2)
    db.refresh(entry2)
    assert line2.matched_registry_id == entry2.id


def test_match_registry_and_statement_ignores_missing_account_number(app, db):
    """Записи без account_number не участвуют в параметрическом match —
    не можем однозначно сопоставить."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-20",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number=None,
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-20",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("100.00"),
        account_number=None,
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 0


def test_match_registry_and_statement_manual_route(app, db, client):
    """Ручной запуск сопоставления через POST-роут."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair_match", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_match", "pass12345")

    resp = client.post(f"/cooperative/bank-accounts/{account.id}/match-registry-statement")
    assert resp.status_code == 302


def test_match_registry_and_statement_requires_chairman(app, db, client):
    """Только председатель может запускать сопоставление."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "board_member", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_member", "pass12345")

    resp = client.post(f"/cooperative/bank-accounts/{account.id}/match-registry-statement")
    assert resp.status_code == 302


def test_match_registry_and_statement_direct_preferred_over_parametric(app, db):
    """Если есть прямой match по внешнему ID — он приоритетнее параметрического.
    Даже если параметрический match тоже возможен."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    # Пара с прямым match по external_id
    line_direct = BankStatementLine(
        bank_account_id=account.id, external_uid="direct-001",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number="50001",
    )
    db.add(line_direct)

    entry_direct = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="direct-001",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("100.00"),
        account_number="50001",
    )
    db.add(entry_direct)

    # Пара без прямого match (разные external_id), но с параметрическим (та же сумма и ЛС)
    line_param = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-param-001",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("200.00"),
        account_number="50002",
    )
    db.add(line_param)

    entry_param = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-param-001",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("200.00"),
        account_number="50002",
    )
    db.add(entry_param)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 1
    assert parametric == 1
    db.expire_all()
    assert line_direct.matched_registry_id == entry_direct.id
    assert line_param.matched_registry_id == entry_param.id


def test_match_registry_and_statement_no_cross_match_same_account_number_multiple_amounts(app, db):
    """Если на одном ЛС несколько платежей разных сумм — каждый сопоставляется
    только со своим. Не должно быть cross-match."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    # Выписка: два платежа на один ЛС, разные суммы
    line_a = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-cross-a",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number="60001",
    )
    db.add(line_a)

    line_b = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-cross-b",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("200.00"),
        account_number="60001",
    )
    db.add(line_b)

    # Реестр: те же ЛС и суммы, но разные внешние ID
    entry_a = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-cross-a",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("100.00"),
        account_number="60001",
    )
    db.add(entry_a)

    entry_b = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-cross-b",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("200.00"),
        account_number="60001",
    )
    db.add(entry_b)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 0
    assert parametric == 2
    db.expire_all()
    # Каждая запись реестра сопоставлена с правильной строкой выписки
    assert line_a.matched_registry_id == entry_a.id
    assert line_b.matched_registry_id == entry_b.id
    # Нет перекрёстного сопоставления
    assert line_a.matched_registry_id != line_b.matched_registry_id


def test_match_registry_and_statement_date_diff_exactly_1_day(app, db):
    """Разница в датах ровно 1 день — должно сопоставиться."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-30",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number="70001",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-30",
        operation_date=dt.date(2026, 8, 16), amount=Decimal("100.00"),
        account_number="70001",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert parametric == 1


def test_match_registry_and_statement_date_diff_2_days_no_match(app, db):
    """Разница в датах 2 дня — не сопоставляется."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    line = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-31",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number="70002",
    )
    db.add(line)

    entry = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-31",
        operation_date=dt.date(2026, 8, 17), amount=Decimal("100.00"),
        account_number="70002",
    )
    db.add(entry)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert parametric == 0


def test_match_registry_and_statement_mixed_direct_and_parametric(app, db):
    """Смешанный сценарий: часть пар совпадает по внешнему ID, часть — по параметрам.
    Оба типа должны сработать независимо."""
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    db.flush()

    # Прямая пара
    line_direct = BankStatementLine(
        bank_account_id=account.id, external_uid="mix-direct-1",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("100.00"),
        account_number="80001",
    )
    db.add(line_direct)
    entry_direct = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="mix-direct-1",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("100.00"),
        account_number="80001",
    )
    db.add(entry_direct)

    # Параметрическая пара (разные external_id, но совпадают ЛС и сумма)
    line_param = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-mix-param-1",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("200.00"),
        account_number="80002",
    )
    db.add(line_param)
    entry_param = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-mix-param-1",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("200.00"),
        account_number="80002",
    )
    db.add(entry_param)

    # Третий — нет совпадений (разные external_id и разные ЛС)
    line_none = BankStatementLine(
        bank_account_id=account.id, external_uid="stmt-mix-none-1",
        operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("300.00"),
        account_number="80003",
    )
    db.add(line_none)
    entry_none = PaymentRegistryEntry(
        bank_account_id=account.id, external_id="reg-mix-none-1",
        operation_date=dt.date(2026, 8, 15), amount=Decimal("300.00"),
        account_number="80004",  # другой ЛС
    )
    db.add(entry_none)
    db.commit()

    with app.app_context():
        direct, parametric = bank_sync._match_registry_and_statement(account.id)

    assert direct == 1
    assert parametric == 1
    db.expire_all()
    # Прямая пара
    assert line_direct.matched_registry_id == entry_direct.id
    # Параметрическая пара
    assert line_param.matched_registry_id == entry_param.id
    # Без совпадений
    assert line_none.matched_registry_id is None
    assert entry_none.matched_statement_id is None


def test_upload_payment_registry_triggers_match(app, db, client):
    """Загрузка реестра автоматически запускает сопоставление с существующей выпиской."""
    person = make_person(db)
    garage = make_garage(db)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="90001",
    )
    db.add(member_account)
    db.flush()

    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair_upload_match", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_upload_match", "pass12345")

    # Сначала загружаем выписку с конкретным external_uid
    from app.bank_api.base import StatementLine
    stub = _StubClient(statement_result=[
        StatementLine(
            external_uid="upload-match-001",
            operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("500.00"),
            counterparty_name=person.full_name,
            payment_purpose="ЛС 90001; Членский взнос",
        ),
    ])

    import unittest.mock
    with unittest.mock.patch.object(bank_sync, "get_client", lambda acc: stub):
        resp = client.post(
            f"/cooperative/bank-accounts/{bank_account.id}/sync-statement",
            data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
    assert resp.status_code == 302

    # Теперь загружаем реестр с тем же external_id
    raw = "15-08-2026;10-00-00;0001;0001111V;100200300;90001;Петров Пётр;Взнос;0625;500,00;495,00;5,00;5\r\n"
    from io import BytesIO
    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/upload",
        data={"registry_file": (BytesIO(raw.encode("cp1251")), "registry.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302

    # Проверяем, что запись реестра сопоставлена с выпиской
    db.expire_all()
    line = database.db_session.query(BankStatementLine).filter_by(bank_account_id=bank_account.id).first()
    entry = database.db_session.query(PaymentRegistryEntry).filter_by(bank_account_id=bank_account.id).first()
    assert line.matched_registry_id == entry.id
    assert entry.matched_statement_id == line.id


def test_sync_statement_triggers_match(app, db, client, monkeypatch):
    """Загрузка выписки автоматически запускает сопоставление с существующим реестром."""
    bank_account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_user(db, "chair_sync_match", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_sync_match", "pass12345")

    # Сначала загружаем реестр
    raw = "15-08-2026;10-00-00;0001;0001111V;100200300;90002;Иванов Иван;Взнос;0625;750,00;742,50;7,50;5\r\n"
    from io import BytesIO
    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/registry/payments/upload",
        data={"registry_file": (BytesIO(raw.encode("cp1251")), "registry.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302

    # Теперь загружаем выписку с тем же external_uid
    stub = _StubClient(statement_result=[
        StatementLine(
            external_uid="sync-match-001",
            operation_date=dt.date(2026, 8, 15), direction="credit", amount=Decimal("750.00"),
            counterparty_name="Иванов Иван",
            payment_purpose="ЛС 90002; Членский взнос",
        ),
    ])
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: stub)

    resp = client.post(
        f"/cooperative/bank-accounts/{bank_account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert resp.status_code == 302

    # Проверяем, что выписка сопоставлена с реестром
    db.expire_all()
    line = database.db_session.query(BankStatementLine).filter_by(bank_account_id=bank_account.id).first()
    entry = database.db_session.query(PaymentRegistryEntry).filter_by(bank_account_id=bank_account.id).first()
    assert line.matched_registry_id == entry.id
    assert entry.matched_statement_id == line.id
