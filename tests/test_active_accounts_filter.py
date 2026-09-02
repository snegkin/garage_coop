"""
Чекбокс «Актуальные» (JS, включён по умолчанию) на таблицах лицевых счетов
страницы гаража, страницы человека и общего списка «Финансы» — скрывает на
клиенте архивные счета (MemberAccount.is_archived) и полностью погашенные
пени (вид взноса — пеня, баланс ровно 0). Сама фильтрация — в base.html
(гараж/человек) и в скрипте finance/member_accounts.html (общий список);
здесь проверяем только, что нужная разметка (чекбокс + data-атрибуты
на строках) действительно попадает в HTML.
"""
import datetime as dt
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_fee_types(db):
    regular = FeeType(code="membership", name="Членский взнос", type_code="1")
    penalty = FeeType(code="membership_penalty", name="Пеня по взносу", type_code="1", is_penalty=True)
    db.add_all([regular, penalty])
    db.flush()
    return regular, penalty


def test_garage_detail_has_active_only_checkbox_and_row_markers(app, db, client):
    person = make_person(db, full_name="Проверка Чекбокса")
    garage = make_garage(db, number="300")
    make_ownership(db, garage, person)
    regular, penalty = _setup_fee_types(db)
    regular_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=regular.id, account_number="13000")
    penalty_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П13000")
    db.add_all([regular_acc, penalty_acc])
    db.flush()
    # пеня начислялась и полностью погашена — баланс ровно 0, но не должна
    # исчезать с сервера, только скрываться на клиенте по чекбоксу
    db.add(Charge(account_id=penalty_acc.id, year=2026, amount=Decimal("50.00")))
    db.add(Payment(account_id=penalty_acc.id, date=dt.date(2026, 1, 1), amount=Decimal("50.00")))
    make_user(db, "board_filter", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_filter", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="garageAccountsActiveOnly"' in html
    assert "checked" in html.split('id="garageAccountsActiveOnly"')[1].split(">")[0]
    assert f'data-account-row="{penalty_acc.id}"' in html
    assert 'data-zero-penalty="1"' in html
    assert 'data-archived="0"' in html


def test_garage_detail_shows_penalty_account_with_no_charges_at_all(app, db, client):
    """
    Регрессия: раньше garages.py/persons.py (detail()) отдельно от чекбокса
    ещё и отфильтровывали пени БЕЗ единого начисления ("not is_penalty or
    ma.charges") — такая строка вообще не попадала в HTML ни при каком
    состоянии чекбокса, то есть снять галку «Актуальные» не давало её
    увидеть, хотя баланс такого счёта тоже ровно 0 (нет начислений — нет и
    долга), то есть по смыслу он должен вести себя как «полностью
    погашенная пеня» — быть скрытым по умолчанию, но появляться по снятой
    галке. Фильтр убран, скрытие теперь целиком на чекбоксе.
    """
    person = make_person(db, full_name="Пеня Без Начислений")
    garage = make_garage(db, number="303")
    make_ownership(db, garage, person)
    regular, penalty = _setup_fee_types(db)
    penalty_acc = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=penalty.id, account_number="П13030")
    db.add(penalty_acc)
    make_user(db, "board_filter4", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_filter4", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'data-account-row="{penalty_acc.id}"' in html
    assert 'data-zero-penalty="1"' in html

    resp2 = client.get(f"/persons/{person.id}")
    assert resp2.status_code == 200
    html2 = resp2.get_data(as_text=True)
    assert f'data-account-row="{penalty_acc.id}"' in html2
    assert 'data-zero-penalty="1"' in html2


def test_person_detail_has_active_only_checkbox_and_archived_marker(app, db, client):
    old_owner = make_person(db, full_name="Архивный Собственник")
    new_owner = make_person(db, full_name="Новый Собственник")
    garage = make_garage(db, number="301")
    ownership = make_ownership(db, garage, old_owner)
    regular, _ = _setup_fee_types(db)
    old_account = MemberAccount(person_id=old_owner.id, garage_id=garage.id, fee_type_id=regular.id, account_number="13010")
    db.add(old_account)
    make_user(db, "board_filter2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_filter2", "pass12345")

    # выбытие единственного собственника, затем новый — старый счёт архивируется
    client.post(f"/garages/{garage.id}/owners/{ownership.id}/remove", data={"comment": "продал"})
    client.post(f"/garages/{garage.id}/owners/add", data={"person_id": new_owner.id, "share": "1"})
    db.expire_all()

    resp = client.get(f"/persons/{old_owner.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="personAccountsActiveOnly"' in html
    assert 'data-archived="1"' in html
    assert "архив" in html  # бейдж архивного счёта


def test_finance_member_accounts_has_active_only_checkbox(app, db, client):
    person = make_person(db, full_name="Финансовая Проверка")
    garage = make_garage(db, number="302")
    make_ownership(db, garage, person)
    regular, _ = _setup_fee_types(db)
    db.add(MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=regular.id, account_number="13020"))
    make_user(db, "board_filter3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_filter3", "pass12345")

    resp = client.get("/finance/member-accounts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="memberAccountsActiveOnly"' in html
    assert 'data-archived="0"' in html
