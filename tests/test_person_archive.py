"""
Архивация человека (умер, продал гараж и т.п.) — скрытие из общего
реестра с сохранением доступа по прямой ссылке, синхронизированное с
выбытием из собственников гаражей: доли оставшихся пересчитываются,
остаток лицевых счетов выбывшего по каждому такому гаражу переносится
им пропорционально новым долям. Единственный собственник — ничего не
трогается (ни GarageOwnership, ни лицевые счета).

Общее ядро (garages._remove_owner_and_redistribute) используется и
здесь, и обычной ручной кнопкой «Удалить собственника» на странице
гаража — тесты на саму механику переноса/пересчёта (несколько
совладельцев, разные пропорции, долг vs переплата, округление) лежат
в tests/test_garage_ownership_history.py при её наличии; здесь —
именно про архивацию человека и то, что она эту механику вызывает.
"""
import datetime as dt
from decimal import Decimal

from app import database
from app.models import (
    RoleEnum, Person, MemberAccount, FeeType, Charge, Payment, GarageOwnership,
)
from app.accounting import balance, reallocate_member_charges

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_fee_type(db, name="Членский взнос", code="10", type_code="1"):
    ft = FeeType(code=code, name=name, type_code=type_code)
    db.add(ft)
    db.flush()
    return ft


# ---------------------------------------------------------------------------
# Видимость в общем реестре / архиве
# ---------------------------------------------------------------------------

def test_archived_person_hidden_from_default_list(app, db, client):
    person = make_person(db, full_name="Скрытый Архивович")
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair1", "pass12345")

    resp_before = client.get("/persons/")
    assert "Скрытый Архивович" in resp_before.get_data(as_text=True)

    resp = client.post(f"/persons/{person.id}/archive", data={"reason": "выбыл"})
    assert resp.status_code == 302

    resp_after = client.get("/persons/")
    assert "Скрытый Архивович" not in resp_after.get_data(as_text=True)

    resp_archived_tab = client.get("/persons/", query_string={"archived": "1"})
    assert "Скрытый Архивович" in resp_archived_tab.get_data(as_text=True)


def test_archived_person_still_reachable_by_direct_link(app, db, client):
    person = make_person(db, full_name="Доступный По Ссылке")
    make_user(db, "chair2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair2", "pass12345")

    client.post(f"/persons/{person.id}/archive", data={"reason": "умер"})

    resp = client.get(f"/persons/{person.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "умер" in html
    assert "В архиве" in html

    # выписка по счетам тоже доступна
    resp2 = client.get(f"/persons/{person.id}/statement")
    assert resp2.status_code == 200


def test_archiving_twice_is_a_noop_with_warning(app, db, client):
    person = make_person(db, full_name="Дважды Архивируемый")
    make_user(db, "chair3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair3", "pass12345")

    client.post(f"/persons/{person.id}/archive", data={"reason": "умер"})
    reason_before = database.db_session.get(Person, person.id).archived_reason

    client.post(f"/persons/{person.id}/archive", data={"reason": "другая причина"})
    db.expire_all()
    # вторая попытка не должна перезаписать причину — просто предупреждение
    assert database.db_session.get(Person, person.id).archived_reason == reason_before


def test_unarchive_restores_visibility(app, db, client):
    person = make_person(db, full_name="Вернувшийся Из Архива")
    make_user(db, "chair4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair4", "pass12345")

    client.post(f"/persons/{person.id}/archive", data={"reason": "умер"})
    resp = client.post(f"/persons/{person.id}/unarchive")
    assert resp.status_code == 302

    db.expire_all()
    updated = database.db_session.get(Person, person.id)
    assert updated.is_archived is False
    assert updated.archived_reason is None

    resp2 = client.get("/persons/")
    assert "Вернувшийся Из Архива" in resp2.get_data(as_text=True)


def test_only_board_can_archive(app, db, client):
    person = make_person(db, full_name="Защищённый От Рядовых")
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.post(f"/persons/{person.id}/archive", data={"reason": "умер"})
    assert resp.status_code == 302  # редирект — доступ запрещён
    db.expire_all()
    assert database.db_session.get(Person, person.id).is_archived is False


# ---------------------------------------------------------------------------
# Синхронизация с собственниками гаража — сама механика
# ---------------------------------------------------------------------------

def test_archiving_sole_owner_leaves_garage_and_accounts_untouched(app, db, client):
    person = make_person(db, full_name="Единственный Собственник")
    garage = make_garage(db, number="1")
    make_ownership(db, garage, person, share="1")
    fee_type = _make_fee_type(db)
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10001")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("500.00")))
    db.flush()
    reallocate_member_charges(account)
    balance_before = balance(account)

    make_user(db, "chair5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair5", "pass12345")

    client.post(f"/persons/{person.id}/archive", data={"reason": "умер, наследников пока нет"})

    db.expire_all()
    # GarageOwnership удалена (гараж формально без собственника)...
    remaining = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).all()
    assert remaining == []
    # ...но лицевой счёт остался, баланс не тронут
    still_there = database.db_session.get(MemberAccount, account.id)
    assert still_there is not None
    assert balance(still_there) == balance_before == Decimal("-500.00")


def test_archiving_co_owner_redistributes_balance_and_shares(app, db, client):
    person_a = make_person(db, full_name="Выбывающий Совладелец")
    person_b = make_person(db, full_name="Остающийся Совладелец")
    garage = make_garage(db, number="2")
    make_ownership(db, garage, person_a, share="0.5")
    make_ownership(db, garage, person_b, share="0.5")
    fee_type = _make_fee_type(db)
    account_a = MemberAccount(person_id=person_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="2A")
    account_b = MemberAccount(person_id=person_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="2B")
    db.add_all([account_a, account_b])
    db.flush()
    # У A переплата 300.00
    db.add(Payment(account_id=account_a.id, date=dt.date(2026, 1, 1), amount=Decimal("300.00")))
    db.flush()
    reallocate_member_charges(account_a)
    assert balance(account_a) == Decimal("300.00")

    make_user(db, "chair6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair6", "pass12345")

    client.post(f"/persons/{person_a.id}/archive", data={"reason": "продал долю"})

    db.expire_all()
    remaining = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).all()
    assert len(remaining) == 1
    assert remaining[0].person_id == person_b.id
    assert remaining[0].share == Decimal("1.00000")

    assert balance(database.db_session.get(MemberAccount, account_a.id)) == Decimal("0.00")
    assert balance(database.db_session.get(MemberAccount, account_b.id)) == Decimal("300.00")
    # счёт выбывшего не удалён — история доступна
    assert database.db_session.get(MemberAccount, account_a.id) is not None

    # платёж, унаследованный оставшимся содольщиком, ссылается на выбывшего
    # (см. accounting.redistribute_member_account_balance,
    # Payment.related_person_id) — по этой ссылке в HTML имя выбывшего
    # становится кликабельным для правления (app/comment_format.py)
    inherited_payment = next(
        p for p in database.db_session.get(MemberAccount, account_b.id).payments
        if p.related_person_id is not None
    )
    assert inherited_payment.related_person_id == person_a.id
    assert "Выбывающий Совладелец" in inherited_payment.comment


def test_archiving_co_owner_with_debt_transfers_debt_not_credit(app, db, client):
    """Симметричный случай — у выбывающего был ДОЛГ, не переплата; он
    должен перейти оставшемуся, а не создать ему переплату."""
    person_a = make_person(db, full_name="Должник При Архивации")
    person_b = make_person(db, full_name="Наследующий Долг")
    garage = make_garage(db, number="3")
    make_ownership(db, garage, person_a, share="0.5")
    make_ownership(db, garage, person_b, share="0.5")
    fee_type = _make_fee_type(db)
    account_a = MemberAccount(person_id=person_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="3A")
    account_b = MemberAccount(person_id=person_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="3B")
    db.add_all([account_a, account_b])
    db.flush()
    db.add(Charge(account_id=account_a.id, year=2026, amount=Decimal("777.00")))
    db.flush()
    reallocate_member_charges(account_a)
    assert balance(account_a) == Decimal("-777.00")

    make_user(db, "chair7", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair7", "pass12345")

    client.post(f"/persons/{person_a.id}/archive", data={"reason": "продал, долг остаётся за гаражом"})

    db.expire_all()
    assert balance(database.db_session.get(MemberAccount, account_a.id)) == Decimal("0.00")
    assert balance(database.db_session.get(MemberAccount, account_b.id)) == Decimal("-777.00")


def test_archiving_co_owner_with_three_owners_uneven_shares(app, db, client):
    """Неровные доли (0.5/0.3/0.2) — проверка, что перераспределение и
    пересчёт долей делаются пропорционально, без потери копейки на
    округлении (100.00 распределяется как 60.00+40.00, а не 59.99+40.00
    или подобное)."""
    person_a = make_person(db, full_name="Выбывающий Из Трёх")
    person_b = make_person(db, full_name="Первый Из Оставшихся")
    person_c = make_person(db, full_name="Второй Из Оставшихся")
    garage = make_garage(db, number="4")
    make_ownership(db, garage, person_a, share="0.5")
    make_ownership(db, garage, person_b, share="0.3")
    make_ownership(db, garage, person_c, share="0.2")
    fee_type = _make_fee_type(db)
    account_a = MemberAccount(person_id=person_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="4A")
    account_b = MemberAccount(person_id=person_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="4B")
    account_c = MemberAccount(person_id=person_c.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="4C")
    db.add_all([account_a, account_b, account_c])
    db.flush()
    db.add(Payment(account_id=account_a.id, date=dt.date(2026, 1, 1), amount=Decimal("100.00")))
    db.flush()
    reallocate_member_charges(account_a)

    make_user(db, "chair8", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair8", "pass12345")

    client.post(f"/persons/{person_a.id}/archive", data={"reason": "умер"})

    db.expire_all()
    ownerships = {
        o.person_id: o.share for o in
        database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).all()
    }
    assert ownerships[person_b.id] == Decimal("0.60000")  # 0.3 / 0.5
    assert ownerships[person_c.id] == Decimal("0.40000")  # 0.2 / 0.5
    assert sum(ownerships.values()) == Decimal("1.00000")

    balance_b = balance(database.db_session.get(MemberAccount, account_b.id))
    balance_c = balance(database.db_session.get(MemberAccount, account_c.id))
    assert balance_b == Decimal("60.00")
    assert balance_c == Decimal("40.00")
    assert balance_b + balance_c == Decimal("100.00")  # копейка не потерялась


def test_archiving_multiple_garages_at_once(app, db, client):
    """Человек — совладелец сразу двух гаражей: архивация должна убрать
    его из собственников ОБОИХ, не только первого попавшегося."""
    person = make_person(db, full_name="Совладелец Двух Гаражей")
    co_owner_1 = make_person(db, full_name="Сособственник Первого")
    co_owner_2 = make_person(db, full_name="Сособственник Второго")
    garage1 = make_garage(db, number="5")
    garage2 = make_garage(db, number="6")
    make_ownership(db, garage1, person, share="0.5")
    make_ownership(db, garage1, co_owner_1, share="0.5")
    make_ownership(db, garage2, person, share="0.5")
    make_ownership(db, garage2, co_owner_2, share="0.5")

    make_user(db, "chair9", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair9", "pass12345")

    client.post(f"/persons/{person.id}/archive", data={"reason": "умер"})

    db.expire_all()
    remaining1 = database.db_session.query(GarageOwnership).filter_by(garage_id=garage1.id).all()
    remaining2 = database.db_session.query(GarageOwnership).filter_by(garage_id=garage2.id).all()
    assert len(remaining1) == 1 and remaining1[0].person_id == co_owner_1.id
    assert len(remaining2) == 1 and remaining2[0].person_id == co_owner_2.id


# ---------------------------------------------------------------------------
# Архивация прямо с карточки гаража (не только с карточки человека) — см.
# app/templates/garages/detail.html: кнопка «В архив» у каждого
# собственника (кроме уже архивных), рядом с «Изменить долю»/«Удалить
# собственника», делает то же самое, что и archive_person с карточки
# человека, но после сохранения возвращает на страницу гаража (hidden
# поле next, тот же приём, что у persons.create).
# ---------------------------------------------------------------------------

def test_garage_page_shows_archive_button_for_each_active_owner(app, db, client):
    garage = make_garage(db, number="36")
    owner1 = make_person(db, full_name="Иванов Иван Иванович")
    owner2 = make_person(db, full_name="Петров Пётр Петрович")
    make_ownership(db, garage, owner1, share="0.5")
    make_ownership(db, garage, owner2, share="0.5")
    make_user(db, "chair10", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair10", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'action="/persons/{owner1.id}/archive"' in body
    assert f'action="/persons/{owner2.id}/archive"' in body
    assert f'value="/garages/{garage.id}"' in body


def test_archiving_owner_from_garage_page_redirects_back_to_garage(app, db, client):
    garage = make_garage(db, number="36")
    owner1 = make_person(db, full_name="Иванов Иван Иванович")
    owner2 = make_person(db, full_name="Петров Пётр Петрович")
    make_ownership(db, garage, owner1, share="0.5")
    make_ownership(db, garage, owner2, share="0.5")
    make_user(db, "chair11", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair11", "pass12345")

    resp = client.post(f"/persons/{owner1.id}/archive", data={
        "reason": "умер",
        "next": f"/garages/{garage.id}",
    })
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/garages/{garage.id}"

    db.expire_all()
    p = database.db_session.get(Person, owner1.id)
    assert p.is_archived is True
    remaining = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).all()
    assert len(remaining) == 1 and remaining[0].person_id == owner2.id


def test_archive_ignores_unsafe_next_url(app, db, client):
    """next — обычная защита от open redirect (см. auth.is_safe_next_url):
    внешний домен игнорируется, откатываемся на карточку человека."""
    person = make_person(db, full_name="Проверка Редиректа")
    make_user(db, "chair12", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair12", "pass12345")

    resp = client.post(f"/persons/{person.id}/archive", data={
        "reason": "выбыл",
        "next": "https://evil.example.com/",
    })
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/persons/{person.id}"


def test_archive_button_hidden_for_already_archived_owner(app, db, client):
    garage = make_garage(db, number="36")
    owner = make_person(db, full_name="Уже В Архиве")
    make_ownership(db, garage, owner, share="1")
    make_user(db, "chair13", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair13", "pass12345")

    client.post(f"/persons/{owner.id}/archive", data={"reason": "умер"})
    # Единственный собственник — GarageOwnership не тронут (см. docstring
    # _remove_owner_and_redistribute), поэтому архивный человек всё ещё
    # виден в таблице собственников гаража, но кнопку «В архив» ему
    # предлагать больше не нужно.
    resp = client.get(f"/garages/{garage.id}")
    body = resp.get_data(as_text=True)
    assert owner.full_name in body
    assert f'action="/persons/{owner.id}/archive"' not in body
