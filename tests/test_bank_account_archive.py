"""Реестры — только у счетов в Сбербанке; архив и удаление расчётных
счетов (BankAccount.is_archived, cooperative.archive/delete_bank_account)."""
import datetime as dt
from decimal import Decimal

from app import database
from app.accounting import cooperative_balance, get_primary_bank_account
from app.models import BankAccount, BankApiProvider, BankStatementLine, ChargeRegistryFile, PersonalAccount, RoleEnum
from tests.conftest import make_garage, make_user, login


def _account(db, bank_name, provider=BankApiProvider.NONE, number="40703810000000000001", **kwargs):
    account = BankAccount(bank_name=bank_name, checking_account=number, api_provider=provider, **kwargs)
    db.add(account)
    db.flush()
    return account


def _login_chairman(db, client):
    make_user(db, "chair_arch", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_arch", "pass12345")


def test_is_sberbank_by_api_or_bank_name(app, db):
    assert _account(db, "Любой", BankApiProvider.SBERBANK).is_sberbank
    assert _account(db, "КИРОВСКОЕ ОТДЕЛЕНИЕ N8612 ПАО СБЕРБАНК", number="2").is_sberbank
    assert not _account(db, "АО КБ «Модульбанк»", BankApiProvider.MODULBANK, number="3").is_sberbank
    archived = _account(db, "ПАО СБЕРБАНК", number="4", is_archived=True)
    assert archived.is_sberbank and not archived.registries_available


def test_registries_hidden_and_404_for_non_sberbank(app, db, client):
    modul = _account(db, "Модульбанк", BankApiProvider.MODULBANK)
    sber = _account(db, "ПАО СБЕРБАНК", BankApiProvider.SBERBANK, number="40703810000000000002")
    _login_chairman(db, client)

    for path in ("registry/charges", "registry/payments", "registry/format"):
        assert client.get(f"/cooperative/bank-accounts/{modul.id}/{path}").status_code == 404
        assert client.get(f"/cooperative/bank-accounts/{sber.id}/{path}").status_code == 200

    page = client.get("/finance/bank-accounts").get_data(as_text=True)
    assert f"/cooperative/bank-accounts/{sber.id}/registry/charges" in page
    assert f"/cooperative/bank-accounts/{modul.id}/registry/" not in page
    assert f"/cooperative/bank-accounts/{modul.id}/statement" in page  # выписка — как была


def test_archived_account_leaves_balance_primary_and_registry_links(app, db, client):
    old = _account(db, "ПАО СБЕРБАНК", BankApiProvider.SBERBANK, is_primary=True, balance=Decimal("100.00"))
    new = _account(db, "Модульбанк", BankApiProvider.MODULBANK, number="40703810000000000002", balance=Decimal("50.00"))
    _login_chairman(db, client)

    client.post(f"/cooperative/bank-accounts/{old.id}/archive")
    database.db_session.refresh(old)
    assert old.is_archived and not old.is_primary
    assert cooperative_balance() == Decimal("50.00")
    assert get_primary_bank_account().id == new.id
    page = client.get("/finance/bank-accounts").get_data(as_text=True)
    assert "registry/charges" not in page and "Вернуть из архива" in page

    client.post(f"/cooperative/bank-accounts/{old.id}/unarchive")
    database.db_session.refresh(old)
    assert not old.is_archived
    assert cooperative_balance() == Decimal("150.00")


def test_dashboard_skips_stale_registry_of_archived_account(app, db, client):
    old = _account(db, "ПАО СБЕРБАНК", BankApiProvider.SBERBANK, is_archived=True)
    db.add(ChargeRegistryFile(
        bank_account_id=old.id, filename="r.txt", content=b"", rows_count=0, debtors_count=0, total_amount=Decimal("0"),
        created_at=dt.datetime(2026, 1, 1),
    ))
    # Счёт в пустом реестре → реестр устарел (новый лицевой счёт).
    db.add(PersonalAccount(garage_id=make_garage(db).id, account_number="90123"))
    _login_chairman(db, client)
    assert "Реестр начислений (" not in client.get("/dashboard").get_data(as_text=True)

    old.is_archived = False  # контроль: у действующего счёта напоминание есть
    database.db_session.commit()
    assert "Реестр начислений (" in client.get("/dashboard").get_data(as_text=True)


def test_delete_only_without_history(app, db, client):
    empty = _account(db, "Ошибочно заведённый")
    used = _account(db, "ПАО СБЕРБАНК", number="40703810000000000002")
    db.add(BankStatementLine(
        bank_account_id=used.id, operation_date=dt.date(2026, 11, 1), amount=Decimal("10.00"), direction="credit",
    ))
    _login_chairman(db, client)
    page = client.get("/finance/bank-accounts").get_data(as_text=True)
    assert f"/cooperative/bank-accounts/{empty.id}/delete" in page
    assert f"/cooperative/bank-accounts/{used.id}/delete" not in page

    client.post(f"/cooperative/bank-accounts/{used.id}/delete")
    client.post(f"/cooperative/bank-accounts/{empty.id}/delete")
    remaining = {a.id for a in database.db_session.query(BankAccount)}
    assert remaining == {used.id}
    assert database.db_session.query(BankStatementLine).count() == 1
