"""
Зачёт средств между лицевыми счетами (finance.transfer_member_account_funds)
— ручное исправление ошибочного разнесения платежа (зачислили не на тот
вид взноса) или случая, когда один человек фактически заплатил за
другого. Разрешено только между счетами ОДНОГО И ТОГО ЖЕ человека или
ОДНОГО И ТОГО ЖЕ гаража — не между произвольными людьми (предохранитель
от случайного перевода денег постороннему). Механика — та же
компенсирующая проводка (Charge на счёте-источнике, Payment на
счёте-получателе), что уже используется в accounting.py для передачи
долга/переплаты и в finance.write_off_penalty; доступ — is_board() (как у
«Начислить»/«Зарегистрировать платёж» — кооператив ничего не теряет, это
не то же самое, что списание пени).
"""
import datetime as dt
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, AuditLog

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_fee_types(db):
    membership = FeeType(code="membership", name="Членский взнос", type_code="1")
    land_tax = FeeType(code="land_tax", name="Земельный налог", type_code="2")
    db.add_all([membership, land_tax])
    db.flush()
    return membership, land_tax


def test_transfer_between_accounts_of_same_person(app, db, client):
    """Разнесли платёж не туда: зачислили на взнос вместо налога того же человека."""
    person = make_person(db, full_name="Иванов Иван Иванович")
    garage = make_garage(db, number="80")
    make_ownership(db, garage, person)
    membership, land_tax = _setup_fee_types(db)

    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="18001")
    target = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="18002")
    db.add_all([source, target])
    db.flush()
    db.add(Payment(account_id=source.id, date=dt.date(2026, 1, 1), amount=Decimal("500.00")))  # ошибочно сюда
    db.add(Charge(account_id=target.id, year=2026, amount=Decimal("500.00")))  # а нужно было на налог

    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.post(f"/finance/member-accounts/{source.id}/transfer", data={
        "target_account_id": str(target.id),
        "amount": "500.00",
        "reason": "Разнесение по ошибке ушло не туда",
    })
    assert resp.status_code == 302

    db.expire_all()
    charges = db.query(Charge).filter_by(account_id=source.id).all()
    payments = db.query(Payment).filter_by(account_id=target.id).all()
    assert len(charges) == 1
    assert charges[0].amount == Decimal("500.00")
    assert charges[0].related_person_id == person.id
    assert "18002" in charges[0].comment
    # в комментарии, который видит член кооператива, — инициалы (Person.short_name),
    # не полное ФИО (оно остаётся только в записи журнала аудита ниже)
    assert "Иванов И.И." in charges[0].comment
    assert "Иванов Иван Иванович" not in charges[0].comment
    assert len(payments) == 1  # платёж от зачёта (исходный Payment был на счёте-источнике, не здесь)
    assert payments[0].amount == Decimal("500.00")
    assert "18001" in payments[0].comment
    assert "Иванов И.И." in payments[0].comment
    assert payments[0].related_person_id == person.id

    entries = db.query(AuditLog).filter_by(action="member_account.transfer").all()
    assert len(entries) == 1
    assert "18001" in entries[0].summary and "18002" in entries[0].summary


def test_transfer_between_accounts_of_same_garage_different_persons(app, db, client):
    """Заплатил один сособственник, а зачесть нужно на счёт другого — тот же гараж."""
    owner_a = make_person(db, full_name="Собственник Первый")
    owner_b = make_person(db, full_name="Собственник Второй")
    garage = make_garage(db, number="81")
    make_ownership(db, garage, owner_a, share="0.5")
    make_ownership(db, garage, owner_b, share="0.5")
    membership, _land_tax = _setup_fee_types(db)

    account_a = MemberAccount(person_id=owner_a.id, garage_id=garage.id, fee_type_id=membership.id, account_number="18101")
    account_b = MemberAccount(person_id=owner_b.id, garage_id=garage.id, fee_type_id=membership.id, account_number="18102")
    db.add_all([account_a, account_b])
    db.flush()
    db.add(Payment(account_id=account_a.id, date=dt.date(2026, 1, 1), amount=Decimal("300.00")))

    make_user(db, "board2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board2", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account_a.id}/transfer", data={
        "target_account_id": str(account_b.id),
        "amount": "300.00",
    })
    assert resp.status_code == 302

    db.expire_all()
    assert db.query(Charge).filter_by(account_id=account_a.id).count() == 1
    assert db.query(Payment).filter_by(account_id=account_b.id).count() == 1


def test_transfer_rejected_between_unrelated_accounts(app, db, client):
    """Разные люди и разные гаражи — предохранитель от перевода постороннему."""
    person_a = make_person(db, full_name="Чужой Первый")
    person_b = make_person(db, full_name="Чужой Второй")
    garage_a = make_garage(db, number="82")
    garage_b = make_garage(db, number="83")
    make_ownership(db, garage_a, person_a)
    make_ownership(db, garage_b, person_b)
    membership, _land_tax = _setup_fee_types(db)

    account_a = MemberAccount(person_id=person_a.id, garage_id=garage_a.id, fee_type_id=membership.id, account_number="18201")
    account_b = MemberAccount(person_id=person_b.id, garage_id=garage_b.id, fee_type_id=membership.id, account_number="18202")
    db.add_all([account_a, account_b])
    db.flush()

    make_user(db, "board3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board3", "pass12345")

    resp = client.post(f"/finance/member-accounts/{account_a.id}/transfer", data={
        "target_account_id": str(account_b.id),
        "amount": "100.00",
    })
    assert resp.status_code == 302
    assert db.query(Charge).filter_by(account_id=account_a.id).count() == 0
    assert db.query(Payment).filter_by(account_id=account_b.id).count() == 0

    # соответственно и в выпадающем списке модалки зачёта такой счёт не
    # предлагается (номер может легитимно попасть на страницу через
    # навигацию «Вперёд»/«Назад» по всем счетам — проверяем именно modal)
    resp2 = client.get(f"/finance/member-accounts/{account_a.id}")
    assert resp2.status_code == 200
    body = resp2.get_data(as_text=True)
    assert "transferFundsModal" not in body  # переносить некуда вообще — кнопки нет


def test_transfer_only_for_board(db, client):
    person = make_person(db, full_name="Рядовой Член")
    garage = make_garage(db, number="84")
    make_ownership(db, garage, person)
    membership, land_tax = _setup_fee_types(db)
    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="18301")
    target = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="18302")
    db.add_all([source, target])
    db.flush()
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post(f"/finance/member-accounts/{source.id}/transfer", data={
        "target_account_id": str(target.id),
        "amount": "50.00",
    })
    assert resp.status_code == 403
    assert db.query(Charge).filter_by(account_id=source.id).count() == 0


def test_transfer_modal_lists_accounts_sorted_by_number_with_balances(app, db, client):
    """Счета в выпадающем списке — по возрастанию номера счёта (не по ФИО),
    и рядом с каждым — его текущий баланс, чтобы видно было, куда зачисляются
    средства, не открывая отдельно каждый счёт."""
    person = make_person(db, full_name="Ясенев Ясен Ясенович")
    garage = make_garage(db, number="86")
    make_ownership(db, garage, person)
    membership, land_tax = _setup_fee_types(db)

    source = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="18503")
    target_low = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=land_tax.id, account_number="18501")
    fee_type_c = FeeType(code="dues3", name="Целевой взнос")
    db.add(fee_type_c)
    db.flush()
    target_mid = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type_c.id, account_number="18502")
    db.add_all([source, target_low, target_mid])
    db.flush()
    db.add(Charge(account_id=target_low.id, year=2026, amount=Decimal("150.00")))  # баланс -150

    make_user(db, "board5", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board5", "pass12345")

    resp = client.get(f"/finance/member-accounts/{source.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    pos_18501 = body.index("18501")
    pos_18502 = body.index("18502")
    assert pos_18501 < pos_18502  # по номеру счёта, а не по ФИО (у всех троих оно одинаковое)
    assert "-150,00" in body or "-150.00" in body  # баланс счёта 18501 виден в модалке


def test_transfer_button_hidden_without_transferable_accounts(app, db, client):
    """Единственный счёт у человека/гаража — переносить некуда, кнопки нет."""
    person = make_person(db, full_name="Единственный Счёт")
    garage = make_garage(db, number="85")
    make_ownership(db, garage, person)
    membership, _land_tax = _setup_fee_types(db)
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=membership.id, account_number="18401")
    db.add(account)
    make_user(db, "board4", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board4", "pass12345")

    resp = client.get(f"/finance/member-accounts/{account.id}")
    assert resp.status_code == 200
    assert "transferFundsModal" not in resp.get_data(as_text=True)
