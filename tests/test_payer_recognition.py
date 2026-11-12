"""
Как узнаём плательщика по выписке без реестра платежей (Модульбанк):
1) номер ЛС в начале назначения ПД-4 и её QR-кода;
2) память по счёту плательщика (bank_sync._resolve_account_by_payer_account);
3) разбор ФИО: хвост «//адрес//», плательщик-банк с ФИО в назначении,
   электричество по слову в назначении.
"""
import datetime as dt
from decimal import Decimal

import pytest

from app import bank_sync, database
from app.accounting import balance, pd4_purpose, pd4_qr_payload
from app.bank_api.base import StatementLine
from app.models import (
    BankAccount, BankApiProvider, BankStatementLine, Charge, Cooperative, FeeType, MemberAccount,
    PersonalAccount, RoleEnum,
)
from tests.conftest import make_garage, make_ownership, make_person, make_user, login

PAYER_ACC = "40817810500000012345"   # личный счёт жены члена кооператива
TRANSIT_ACC = "30232810000000000001"  # транзитный счёт банка


class _Stub:
    def __init__(self, lines):
        self.lines = lines

    def get_statement(self, date_from, date_to):
        return self.lines


@pytest.fixture
def coop(db):
    """Член кооператива Иванов: членский взнос (10640), земельный налог
    (20640) и электричество гаража №12 (91200), долги по всем трём."""
    person = make_person(db, full_name="Иванов Иван Иванович")
    garage = make_garage(db, number="12")
    make_ownership(db, garage, person)
    dues = FeeType(code="10", name="Членский взнос")
    land = FeeType(code="20", name="Земельный налог")
    db.add_all([dues, land])
    db.flush()
    dues_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=dues.id, account_number="10640")
    land_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land.id, account_number="20640")
    db.add_all([dues_acc, land_acc, PersonalAccount(garage_id=garage.id, account_number="91200")])
    db.flush()
    db.add_all([
        Charge(account_id=dues_acc.id, year=2026, amount=Decimal("1710.00")),
        Charge(account_id=land_acc.id, year=2026, amount=Decimal("850.00")),
        Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00")),
    ])
    bank_account = BankAccount(
        bank_name="Модульбанк", checking_account="40703810770010000001", api_provider=BankApiProvider.MODULBANK,
    )
    db.add(bank_account)
    db.flush()
    return {"person": person, "garage": garage, "dues": dues_acc, "land": land_acc, "bank": bank_account}


def _sync(db, monkeypatch, bank_account, lines):
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: _Stub(lines))
    status, _msg, stats = bank_sync.sync_account_statement(bank_account, dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    assert status == "success"
    db.expire_all()
    return stats


def _line(uid, purpose, payer="Иванова Мария Петровна", account=PAYER_ACC, amount="1710.00"):
    return StatementLine(
        external_uid=uid, operation_date=dt.date(2026, 11, 5), direction="credit", amount=Decimal(amount),
        counterparty_name=payer, payment_purpose=purpose, counterparty_account=account,
    )


def _row(db, uid):
    return db.query(BankStatementLine).filter_by(external_uid=uid).one()


def _target_number(row):
    payment = row.matched_payment
    if payment is None:
        return None
    return payment.account.account_number if payment.account_id else payment.garage.account.account_number


# ---------------------------------------------------------------------------
# 1. Назначение ПД-4
# ---------------------------------------------------------------------------

def test_pd4_purpose_puts_account_number_first():
    assert pd4_purpose("10640", "Членский взнос", "12") == "ЛС 10640 Членский взнос, гараж №12"
    assert pd4_purpose("10640", "Членский взнос") == "ЛС 10640 Членский взнос"
    assert bank_sync.extract_account_number(pd4_purpose("10640", "Членский взнос", "12")) == "10640"


def test_pd4_purpose_strips_characters_banks_reject():
    """Сбербанк не принимает перевод с «;», «|» и т.п. в назначении."""
    purpose = pd4_purpose("10640", 'Целевой взнос; "ремонт" | крыша & ворота', "12А")
    assert purpose == "ЛС 10640 Целевой взнос ремонт крыша ворота, гараж №12А"
    assert bank_sync.extract_account_number(purpose) == "10640"
    assert len(pd4_purpose("1", "х" * 500)) == 210


def test_pd4_qr_payload_uses_new_purpose(db, coop):
    co = Cooperative(full_name="ГСК", inn="1234567890", kpp="123456789", ogrn="1234567890123")
    db.add(co)
    db.flush()
    payload = pd4_qr_payload(co, coop["bank"], coop["dues"], Decimal("1710.00"))
    assert "|Purpose=ЛС 10640 Членский взнос, гараж №12|" in payload
    assert "|PersAcc=10640" in payload


def test_printed_slip_shows_new_purpose(db, client, coop):
    make_user(db, "ivanov", "pass12345", role=RoleEnum.MEMBER, person=coop["person"])
    db.add(Cooperative(full_name="ГСК", inn="1234567890", kpp="123456789", ogrn="1234567890123"))
    coop["bank"].is_primary = True
    db.commit()
    login(client, "ivanov", "pass12345")
    body = client.get(f"/pd4/print?account_id={coop['dues'].id}").get_data(as_text=True)
    assert "ЛС 10640 Членский взнос, гараж №12" in body


# ---------------------------------------------------------------------------
# 2. Память по счёту плательщика
# ---------------------------------------------------------------------------

def test_payer_account_remembered_after_manual_allocation(db, client, monkeypatch, coop):
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    # Жена платит без ЛС, её ФИО в справочнике нет — первый раз не разнести.
    _sync(db, monkeypatch, coop["bank"], [_line("op-1", "членские взносы")])
    assert _row(db, "op-1").matched_payment_id is None

    login(client, "chair", "pass12345")
    resp = client.post(
        f"/cooperative/bank-accounts/{coop['bank'].id}/statement/{_row(db, 'op-1').id}/allocate",
        data={"account_number": "10640"},
    )
    assert resp.status_code == 302
    db.expire_all()

    # Дальше — сама, и по виду взноса выбирается нужный счёт того же члена.
    stats = _sync(db, monkeypatch, coop["bank"], [
        _line("op-2", "Земельный налог", amount="850.00"),
        _line("op-3", "за электроэнергию", amount="500.00"),
    ])
    assert stats["auto_allocated"] == 2
    assert _target_number(_row(db, "op-2")) == "20640"
    assert _target_number(_row(db, "op-3")) == "91200"
    assert balance(coop["land"]) == Decimal("0.00")


def test_payer_account_ambiguous_without_fee_hint_stays_manual(db, monkeypatch, coop):
    _sync(db, monkeypatch, coop["bank"], [_line("op-1", "ЛС 10640; членские")])
    assert _target_number(_row(db, "op-1")) == "10640"
    # Ни ЛС, ни вида взноса — у Иванова три счёта, угадывать нельзя.
    _sync(db, monkeypatch, coop["bank"], [_line("op-2", "оплата")])
    assert _row(db, "op-2").matched_payment_id is None


def test_payer_account_learned_within_one_sync(db, monkeypatch, coop):
    stats = _sync(db, monkeypatch, coop["bank"], [
        _line("op-1", "ЛС 10640; членские"),
        _line("op-2", "земельный налог", amount="850.00"),
    ])
    assert stats["auto_allocated"] == 2
    assert _target_number(_row(db, "op-2")) == "20640"


def test_transit_account_is_not_remembered(db, monkeypatch, coop):
    _sync(db, monkeypatch, coop["bank"], [
        _line("op-1", "ЛС 10640; членские", payer="ПАО СБЕРБАНК", account=TRANSIT_ACC),
    ])
    assert _target_number(_row(db, "op-1")) == "10640"
    _sync(db, monkeypatch, coop["bank"], [
        _line("op-2", "земельный налог", payer="ПАО СБЕРБАНК", account=TRANSIT_ACC, amount="850.00"),
    ])
    assert _row(db, "op-2").matched_payment_id is None


def test_unallocation_forgets_payer_account(db, client, monkeypatch, coop):
    make_user(db, "chair", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    _sync(db, monkeypatch, coop["bank"], [_line("op-1", "ЛС 10640; членские")])
    login(client, "chair", "pass12345")
    client.post(f"/cooperative/bank-accounts/{coop['bank'].id}/statement/{_row(db, 'op-1').id}/unallocate")
    db.expire_all()
    assert _row(db, "op-1").matched_payment_id is None
    _sync(db, monkeypatch, coop["bank"], [_line("op-2", "земельный налог", amount="850.00")])
    assert _row(db, "op-2").matched_payment_id is None


def test_archived_account_from_history_is_ignored(db, monkeypatch, coop):
    _sync(db, monkeypatch, coop["bank"], [_line("op-1", "ЛС 10640; членские")])
    # Гараж продан: счета Иванова архивные, с её счёта больше не угадываем.
    coop["dues"].is_archived = True
    coop["land"].is_archived = True
    db.flush()
    _sync(db, monkeypatch, coop["bank"], [_line("op-2", "земельный налог", amount="850.00")])
    row = _row(db, "op-2")
    assert _target_number(row) != "20640"


# ---------------------------------------------------------------------------
# 3. Разбор ФИО
# ---------------------------------------------------------------------------

def test_payer_name_with_address_tail(db, monkeypatch, coop):
    _sync(db, monkeypatch, coop["bank"], [
        _line("op-1", "земельный налог", payer="ИВАНОВ ИВАН ИВАНОВИЧ//Г. ЯРОСЛАВЛЬ, УЛ. ЛЕНИНА, 1//", amount="850.00"),
    ])
    assert _target_number(_row(db, "op-1")) == "20640"


def test_bank_as_payer_takes_full_name_from_purpose(db, monkeypatch, coop):
    _sync(db, monkeypatch, coop["bank"], [
        _line("op-1", "Перевод от ИВАНОВ ИВАН ИВАНОВИЧ. Земельный налог", payer="АО «ТБАНК»",
              account=TRANSIT_ACC, amount="850.00"),
    ])
    assert _target_number(_row(db, "op-1")) == "20640"


def test_electricity_word_selects_garage_account(db, monkeypatch, coop):
    _sync(db, monkeypatch, coop["bank"], [
        _line("op-1", "Оплата за электроэнергию", payer="Иванов Иван Иванович", account=None, amount="500.00"),
    ])
    assert _target_number(_row(db, "op-1")) == "91200"


def test_modulbank_operation_carries_payer_account():
    from app.bank_api.modulbank import _parse_operation
    line = _parse_operation({
        "id": "x", "status": "Received", "category": "Debet", "currency": "RUR", "amount": 100,
        "executed": "2026-11-05T00:00:00", "contragentBankAccountNumber": "4081 7810 5000 0001 2345",
    })
    assert line.counterparty_account == PAYER_ACC


def test_pd4_qr_payload_strips_field_separator(db, coop):
    co = Cooperative(full_name="ГСК | Ромашка", inn="1234567890", kpp="123456789", ogrn="1234567890123")
    db.add(co)
    db.flush()
    payload = pd4_qr_payload(co, coop["bank"], coop["dues"], Decimal("100"))
    assert "|Name=ГСК Ромашка|" in payload
    assert all("=" in part for part in payload.split("|")[1:])
