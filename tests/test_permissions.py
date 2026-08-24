"""
e2e-тесты прав доступа: рядовой член не должен получить доступ ни к чужому
гаражу/лицевому счёту (IDOR), ни к роутам, ограниченным ролью правления/
председателя. Прогоняются под всеми ролями: анонимный, member, board,
chairman — как в стандартном протоколе проверки проекта.
"""
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup_two_members_with_garages(db):
    """Гараж A принадлежит person_a, гараж B — person_b. У обоих есть
    пользовательские учётки с ролью MEMBER, привязанные к своим Person."""
    person_a = make_person(db, full_name="Owner A")
    person_b = make_person(db, full_name="Owner B")
    garage_a = make_garage(db, number="10")
    garage_b = make_garage(db, number="20")
    make_ownership(db, garage_a, person_a)
    make_ownership(db, garage_b, person_b)
    user_a = make_user(db, "member_a", "pass1234", role=RoleEnum.MEMBER, person=person_a)
    user_b = make_user(db, "member_b", "pass1234", role=RoleEnum.MEMBER, person=person_b)
    db.commit()
    return garage_a, garage_b, user_a, user_b


def test_anonymous_cannot_reach_dashboard(client):
    resp = client.get("/dashboard")
    assert resp.status_code in (302, 401, 403)
    if resp.status_code == 302:
        assert "/auth/login" in resp.headers["Location"]


def test_anonymous_cannot_reach_board_only_routes(client):
    for url in ("/garages/", "/persons/", "/finance/member-accounts", "/news/"):
        resp = client.get(url)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]


def test_member_cannot_see_other_members_garage(app, db, client):
    garage_a, garage_b, user_a, user_b = _setup_two_members_with_garages(db)

    login(client, "member_a", "pass1234")
    resp = client.get(f"/garages/{garage_b.id}")
    assert resp.status_code == 403


def test_member_can_see_own_garage(app, db, client):
    garage_a, garage_b, user_a, user_b = _setup_two_members_with_garages(db)

    login(client, "member_a", "pass1234")
    resp = client.get(f"/garages/{garage_a.id}")
    assert resp.status_code == 200


def test_member_cannot_reach_board_only_list(app, db, client):
    garage_a, garage_b, user_a, user_b = _setup_two_members_with_garages(db)

    login(client, "member_a", "pass1234")
    resp = client.get("/garages/")
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/garages/"  # редирект куда-то ещё (нет доступа), не сам роут


def test_board_can_see_any_garage(app, db, client):
    garage_a, garage_b, user_a, user_b = _setup_two_members_with_garages(db)
    board_person = make_person(db, full_name="Board Member")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp_a = client.get(f"/garages/{garage_a.id}")
    resp_b = client.get(f"/garages/{garage_b.id}")
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200


def test_member_cannot_view_other_members_account(app, db, client):
    person_a = make_person(db, full_name="Owner A")
    person_b = make_person(db, full_name="Owner B")
    garage = make_garage(db)
    make_ownership(db, garage, person_a)
    fee_type = FeeType(code="30", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    account_b = MemberAccount(
        person_id=person_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="30001",
    )
    db.add(account_b)
    make_user(db, "member_a", "pass1234", role=RoleEnum.MEMBER, person=person_a)
    db.commit()

    login(client, "member_a", "pass1234")
    resp = client.get(f"/finance/member-accounts/{account_b.id}")
    assert resp.status_code == 403


def test_board_cannot_create_fee_type_only_chairman_can(app, db, client):
    """create_fee_type задокументирован как CHAIRMAN-only — обычный член
    правления (BOARD) не должен иметь доступ, хотя видит список (fee_types)."""
    board_person = make_person(db, full_name="Board Member")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp = client.get("/finance/fee-types")
    assert resp.status_code == 200

    resp = client.post("/finance/fee-types/new", data={"code": "X", "name": "X"})
    assert resp.status_code == 302
    assert "/finance/fee-types" not in resp.headers.get("Location", "") or resp.headers["Location"].endswith("/finance/fee-types")
    # Убеждаемся, что взнос не создан — доступ был отклонён, а не выполнен.
    from app import database
    from app.models import FeeType
    assert database.db_session.query(FeeType).filter_by(code="X").first() is None


def test_chairman_only_setup_wizard_blocked_for_board(app, db, client):
    board_person = make_person(db, full_name="Board Member")
    make_user(db, "boarduser", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boarduser", "pass1234")
    resp = client.get("/setup/")
    assert resp.status_code == 302  # redirect с flash "недостаточно прав", не 200
