"""
Комиссия банка за зачисление — настройка расчётного счёта
(BankAccount.bank_fee_percent), а не кооператива: у каждого банка свой
процент (Сбербанк 1,6%, ВТБ «Простой процент» 1%), и надбавка в сумме к
оплате зависит от того, на какой счёт придёт платёж. ПД-4 и земельный
налог — по основному счёту (его реквизиты на платёжке), реестр начислений —
по счёту, для которого он формируется, разнесение строки выписки на
электричество — за вычетом % счёта, на который пришли деньги.
"""
import datetime as dt
import os
from decimal import Decimal

import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig

from app import bank_sync, database
from app.accounting import (
    amount_with_bank_fee, amount_without_bank_fee, balance, bank_fee_multiplier, compute_land_tax,
)
from app.models import (
    BankAccount, BankStatementLine, Charge, Cooperative, FeeType, MemberAccount, PersonalAccount, RoleEnum,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_coop(db, **kwargs):
    coop = Cooperative(
        full_name="Тестовый кооператив", inn="1234567890", kpp="123456789", ogrn="1234567890123",
        cadastral_area=Decimal("800"), cadastral_value=Decimal("5000000"), common_area=Decimal("500"),
        land_tax_rate_percent=Decimal("1.5"), standard_garage_land_area=Decimal("30"), **kwargs,
    )
    db.add(coop)
    db.flush()
    return coop


def _make_account(db, bank_name, percent, is_primary=False, checking_account="40703810000000000001"):
    account = BankAccount(
        bank_name=bank_name, bik="047888760", checking_account=checking_account,
        is_primary=is_primary, bank_fee_percent=percent,
    )
    db.add(account)
    db.flush()
    return account


def _sber_and_vtb(db, primary="sber"):
    sber = _make_account(db, "ПАО СБЕРБАНК", Decimal("1.6"), is_primary=primary == "sber")
    vtb = _make_account(db, "Банк ВТБ (ПАО)", Decimal("1"), is_primary=primary == "vtb",
                        checking_account="40703810000000000002")
    return sber, vtb


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------

def test_multiplier_without_bank_account_is_one():
    assert bank_fee_multiplier(None) == Decimal("1")
    assert amount_with_bank_fee(Decimal("100.00"), None) == Decimal("100.00")
    assert amount_without_bank_fee(Decimal("100.00"), None) == Decimal("100.00")


def test_amount_without_bank_fee_inverts_amount_with_bank_fee(db):
    """Оплатили ровно «к оплате» по ПД-4 — после вычета % банка остаётся
    ровно долг, при любом проценте и любой сумме (комиссия округляется,
    как у банка, half-up до копейки)."""
    for percent in (Decimal("1"), Decimal("1.6")):
        account = _make_account(db, "Банк", percent)
        for kopecks in range(1, 300001, 13):
            debt = Decimal(kopecks) / 100
            assert amount_without_bank_fee(amount_with_bank_fee(debt, account), account) == debt, (percent, debt)


def test_land_tax_uses_primary_account_fee(db):
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="sber")
    garage = make_garage(db, number="1")
    db.commit()

    with_sber = compute_land_tax(2026)[garage.id]
    sber.is_primary, vtb.is_primary = False, True
    db.commit()
    with_vtb = compute_land_tax(2026)[garage.id]

    # Одна и та же база налога, разные надбавки: / (1 − 1,6%) против / (1 − 1%).
    base_from_sber = with_sber * Decimal("0.984")
    base_from_vtb = with_vtb * Decimal("0.99")
    assert abs(base_from_sber - base_from_vtb) <= Decimal("0.02")
    assert with_vtb < with_sber


# ---------------------------------------------------------------------------
# ПД-4 на электричество — % основного счёта
# ---------------------------------------------------------------------------

def _electricity_owner(db, username):
    person = make_person(db, full_name="Электричество Тестович")
    garage = make_garage(db, number="401")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="40101"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00")))
    make_user(db, username, "pass12345", role=RoleEnum.MEMBER, person=person)
    return garage


def test_electricity_slip_uses_primary_account_fee(db, client):
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="vtb")
    garage = _electricity_owner(db, "elecvtb")
    db.commit()
    login(client, "elecvtb", "pass12345")

    body = client.get(f"/pd4/print?garage_id={garage.id}").get_data(as_text=True)
    assert "1010,10" in body  # 1000 / (1 − 0,01)
    assert "1016,26" not in body  # не процент Сбера
    assert "1,00% - комиссия банка" in body


def test_electricity_slip_follows_primary_account_switch(db, client):
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="sber")
    garage = _electricity_owner(db, "elecswitch")
    db.commit()
    login(client, "elecswitch", "pass12345")

    assert "1016,26" in client.get(f"/pd4/print?garage_id={garage.id}").get_data(as_text=True)
    sber.is_primary, vtb.is_primary = False, True
    db.commit()
    assert "1010,10" in client.get(f"/pd4/print?garage_id={garage.id}").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Реестр начислений — % счёта, для которого он формируется
# ---------------------------------------------------------------------------

def test_charge_registry_items_use_registry_account_fee(app, db):
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="sber")
    garage = make_garage(db, number="7")
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00")))
    db.add(PersonalAccount(garage_id=garage.id, account_number="90070"))
    db.commit()

    sber_item = {i.account_number: i for i in bank_sync._charge_registry_items(sber)}["90070"]
    vtb_item = {i.account_number: i for i in bank_sync._charge_registry_items(vtb)}["90070"]
    assert sber_item.amount == Decimal("1016.26")
    assert sber_item.purpose == "Электричество, гараж №7 (1,60% - комиссия банка)"
    assert vtb_item.amount == Decimal("1010.10")
    assert vtb_item.purpose == "Электричество, гараж №7 (1,00% - комиссия банка)"


# ---------------------------------------------------------------------------
# Разнесение строки выписки — на электричество за вычетом % счёта
# ---------------------------------------------------------------------------

def _statement_line(db, bank_account, amount, purpose, uid):
    line = BankStatementLine(
        bank_account_id=bank_account.id, external_uid=uid, operation_date=dt.date(2026, 11, 5),
        direction="credit", amount=amount, payment_purpose=purpose,
    )
    db.add(line)
    return line


def test_statement_line_to_electricity_credits_amount_net_of_account_fee(db, client):
    """ВТБ не удерживает комиссию из платежа (списывает 1% с поступлений
    отдельно) — в выписке полная оплаченная сумма с надбавкой ПД-4, а
    гаражу должен зачесться ровно долг."""
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="vtb")
    garage = make_garage(db, number="12")
    db.add(PersonalAccount(garage_id=garage.id, account_number="91200"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("1000.00")))
    line = _statement_line(db, vtb, Decimal("1010.10"), "Электричество, гараж №12", "vtb-el-1")
    make_user(db, "chairfee1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chairfee1", "pass12345")

    resp = client.post(
        f"/cooperative/bank-accounts/{vtb.id}/statement/{line.id}/allocate", data={"account_number": "91200"},
    )
    assert resp.status_code == 302
    assert balance(garage) == Decimal("0.00")


def test_statement_line_to_member_account_credits_full_amount(db, client):
    """Взносы: комиссия уже в начислении — разносится полная сумма."""
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="vtb")
    person = make_person(db)
    garage = make_garage(db, number="13")
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10130",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("1500.00")))
    line = _statement_line(db, vtb, Decimal("1500.00"), "Членский взнос", "vtb-mem-1")
    make_user(db, "chairfee2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chairfee2", "pass12345")

    client.post(
        f"/cooperative/bank-accounts/{vtb.id}/statement/{line.id}/allocate", data={"account_number": "10130"},
    )
    assert balance(member_account) == Decimal("0.00")


def test_allocate_all_nets_electricity_by_account_fee(db, client):
    _make_coop(db)
    sber, vtb = _sber_and_vtb(db, primary="vtb")
    garage = make_garage(db, number="14")
    db.add(PersonalAccount(garage_id=garage.id, account_number="91400"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("500.00")))
    _statement_line(db, vtb, Decimal("505.05"), "Оплата по ЛС 91400 электроэнергия", "vtb-el-2")
    make_user(db, "chairfee3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chairfee3", "pass12345")

    client.post(
        f"/cooperative/bank-accounts/{vtb.id}/statement/allocate-all",
        data={"date_from": "2026-11-01", "date_to": "2026-11-30"},
    )
    assert balance(garage) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Формы
# ---------------------------------------------------------------------------

def _bank_account_form(**overrides):
    data = {
        "bank_name": "Банк ВТБ (ПАО)", "bik": "047888760", "checking_account": "40703810000000000009",
        "api_provider": "none", "bank_fee_percent": "1",
    }
    data.update(overrides)
    return data


def test_create_and_edit_bank_account_saves_fee_percent(db, client):
    make_user(db, "chairfee4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chairfee4", "pass12345")

    client.post("/cooperative/bank-accounts/new", data=_bank_account_form())
    account = database.db_session.query(BankAccount).filter_by(checking_account="40703810000000000009").one()
    assert account.bank_fee_percent == Decimal("1")

    client.post(f"/cooperative/bank-accounts/{account.id}/edit", data=_bank_account_form(bank_fee_percent="1,6"))
    db.expire_all()
    assert database.db_session.get(BankAccount, account.id).bank_fee_percent == Decimal("1.6")

    client.post(f"/cooperative/bank-accounts/{account.id}/edit", data=_bank_account_form(bank_fee_percent=""))
    db.expire_all()
    assert database.db_session.get(BankAccount, account.id).bank_fee_percent is None


def test_bank_accounts_page_shows_fee_percent(db, client):
    _sber_and_vtb(db)
    make_user(db, "chairfee5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chairfee5", "pass12345")

    body = client.get("/finance/bank-accounts").get_data(as_text=True)
    assert "1,60%" in body and "1,00%" in body


def test_cooperative_form_no_longer_has_fee_field(db, client):
    _make_coop(db)
    make_user(db, "chairfee6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chairfee6", "pass12345")

    assert 'name="bank_fee_percent"' not in client.get("/cooperative/edit").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Миграция: процент кооператива переезжает на все существующие счета
# ---------------------------------------------------------------------------

def test_migration_moves_coop_fee_to_bank_accounts(tmp_path):
    uri = f"sqlite:///{tmp_path / 'coop.db'}"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = AlembicConfig(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", uri)
    cfg.attributes["db_url_override"] = uri
    cfg.attributes["configure_logger"] = False

    command.upgrade(cfg, "b7e2d4f1a903")
    engine = sa.create_engine(uri)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO cooperative (full_name, inn, kpp, ogrn, land_tax_rate_percent, bank_fee_percent) "
            "VALUES ('ГСК', '1', '1', '1', 1.5, 1.6)"
        ))
        conn.execute(sa.text(
            "INSERT INTO bank_account (bank_name, checking_account, is_primary, api_provider) "
            "VALUES ('Сбербанк', '40703810000000000001', 1, 'NONE')"
        ))

    command.upgrade(cfg, "c4a9e1f37b20")
    with engine.connect() as conn:
        assert float(conn.execute(sa.text("SELECT bank_fee_percent FROM bank_account")).scalar()) == 1.6
        assert "bank_fee_percent" not in {c["name"] for c in sa.inspect(conn).get_columns("cooperative")}

    command.downgrade(cfg, "b7e2d4f1a903")
    with engine.connect() as conn:
        assert float(conn.execute(sa.text("SELECT bank_fee_percent FROM cooperative")).scalar()) == 1.6
    engine.dispose()
