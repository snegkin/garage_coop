"""
Реквизиты счёта кооператива (BankAccount.checking_account/bik/
correspondent_account) в UI обычно вводятся с пробелами-разрядами для
читаемости человеком (как на банковской выписке, напр.
«40703 810 7 7703 0002079»), но поля QR-кода по ГОСТ Р 56042-2014
(PersonalAcc/BIC/CorrespAcc) обязаны быть строго цифрами без разделителей
и той же длины, что настоящий номер счёта (20 цифр у р/с и к/с, 9 у БИК).
Лишние пробелы делали PersonalAcc длиннее 20 символов и невалидным — из-за
этого Сбербанк отказывался принимать такой QR-код («Нельзя оплатить по
этому QR-коду»). Тем же способом (`accounting.account_digits`, Jinja-
глобал) печатается и видимый номер Р/сч на самой квитанции — тоже без
пробелов, по прямой просьбе.
"""
from decimal import Decimal

from app.accounting import pd4_qr_payload_electricity, account_digits
from app.models import Cooperative, RoleEnum, Garage, GarageOwnership, PersonalAccount, Charge, BankAccount

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_coop(db):
    coop = Cooperative(
        full_name="Тестовый кооператив", inn="1234567890", kpp="123456789", ogrn="1234567890123",
    )
    db.add(coop)
    db.flush()
    return coop


def _make_bank_account_with_spaces(db):
    account = BankAccount(
        bank_name="ПАО СБЕРБАНК",
        bik="043304609",
        checking_account="40703 810 7 7703 0002079",  # 24 символа с пробелами, как в реальных реквизитах
        correspondent_account="30101 810 5 0000 0000609",
        is_primary=True,
    )
    db.add(account)
    db.flush()
    return account


def test_account_digits_strips_all_non_digit_characters():
    assert account_digits("40703 810 7 7703 0002079") == "40703810777030002079"
    assert len(account_digits("40703 810 7 7703 0002079")) == 20
    assert account_digits("30101-810-5-0000-0000609") == "30101810500000000609"
    assert account_digits(None) == ""


def test_electricity_qr_payload_personal_acc_has_no_spaces(db):
    coop = _make_coop(db)
    bank_account = _make_bank_account_with_spaces(db)
    person = make_person(db, full_name="Оплатов Оплат Оплатович")
    garage = make_garage(db, number="501")
    make_ownership(db, garage, person)
    account = PersonalAccount(garage_id=garage.id, account_number="50101")
    db.add(account)
    db.flush()
    db.commit()

    payload = pd4_qr_payload_electricity(coop, bank_account, garage, account, Decimal("1000.00"))

    fields = dict(part.split("=", 1) for part in payload.split("|")[1:])
    assert fields["PersonalAcc"] == "40703810777030002079"
    assert len(fields["PersonalAcc"]) == 20
    assert " " not in fields["PersonalAcc"]
    assert fields["BIC"] == "043304609"
    assert fields["CorrespAcc"] == "30101810500000000609"
    assert len(fields["CorrespAcc"]) == 20


def test_electricity_slip_print_page_qr_has_clean_account_digits(db, client):
    coop = _make_coop(db)
    _make_bank_account_with_spaces(db)
    person = make_person(db, full_name="Печатнов Печать Печатнович")
    garage = make_garage(db, number="502")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="50201"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("500.00")))
    make_user(db, "qrowner1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "qrowner1", "pass12345")

    resp = client.get(f"/pd4/print?garage_id={garage.id}")
    assert resp.status_code == 200

    from app.models import PD4Document
    doc = db.query(PD4Document).one()
    fields = dict(part.split("=", 1) for part in doc.qr_payload.split("|")[1:])
    # Только PersonalAcc/BIC/CorrespAcc обязаны быть чистыми цифрами —
    # Name/Purpose/BankName законно содержат пробелы (это не то же самое,
    # что реквизиты счёта, поэтому пробел ищем не по всему payload).
    assert fields["PersonalAcc"] == "40703810777030002079"
    assert " " not in fields["PersonalAcc"]


def test_electricity_slip_print_page_shows_checking_account_without_spaces(db, client):
    """Видимый текст «Р/сч» на самой квитанции — тоже без пробелов, даже
    если в реквизитах кооператива номер внесён с ними для читаемости."""
    coop = _make_coop(db)
    _make_bank_account_with_spaces(db)
    person = make_person(db, full_name="Расчётнов Расчёт Расчётнович")
    garage = make_garage(db, number="503")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="50301"))
    db.add(Charge(garage_id=garage.id, year=2026, amount=Decimal("500.00")))
    make_user(db, "qrowner2", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "qrowner2", "pass12345")

    resp = client.get(f"/pd4/print?garage_id={garage.id}")
    body = resp.get_data(as_text=True)
    assert "40703810777030002079" in body
    assert "40703 810 7 7703 0002079" not in body
