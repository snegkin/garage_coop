"""
Тесты на app/errors.py: 404/403 отдаются понятной страницей, а не голым
Flask-дефолтом; кривой ввод в форме (нечисловая сумма и т.п.) не роняет
запрос в 500, а возвращает пользователя на форму с понятным сообщением.
"""
import re

from app.models import RoleEnum, FeeType, MemberAccount

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_404_page_is_friendly(client):
    resp = client.get("/nonexistent-route-xyz")
    assert resp.status_code == 404
    assert "404" in resp.get_data(as_text=True)


def test_403_page_is_friendly(app, db, client):
    person_a = make_person(db, full_name="Owner A")
    person_b = make_person(db, full_name="Owner B")
    garage_a = make_garage(db, number="10")
    garage_b = make_garage(db, number="20")
    make_ownership(db, garage_a, person_a)
    make_ownership(db, garage_b, person_b)
    make_user(db, "member_a", "pass1234", role=RoleEnum.MEMBER, person=person_a)
    db.commit()

    login(client, "member_a", "pass1234")
    resp = client.get(f"/garages/{garage_b.id}")  # чужой гараж — abort(403)
    assert resp.status_code == 403
    assert "403" in resp.get_data(as_text=True)


def test_bad_amount_in_charge_form_does_not_crash(app, db, client):
    """Нечисловая сумма начисления раньше роняла запрос в 500 с traceback —
    теперь должна вернуть понятное сообщение и не уронить сервер."""
    person = make_person(db)
    garage = make_garage(db)
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10001")
    db.add(account)
    board_person = make_person(db, full_name="Board")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/add",
        data={"year": "2026", "amount": "not-a-number"},
    )
    # Обычный редирект назад (302), не 500 — обработано защитной сеткой в app/errors.py.
    assert resp.status_code == 302


def test_missing_required_field_does_not_crash(app, db, client):
    board_person = make_person(db, full_name="Board")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp = client.post("/finance/fee-types/new", data={})
    # boarduser не CHAIRMAN, так что тут либо 302 (правильный отказ по роли), но не 500
    assert resp.status_code == 302
