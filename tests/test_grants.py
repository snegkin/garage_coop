"""Технические разделы, которые председатель доверяет члену правления (permissions.GRANTS)."""
from app.models import Person, RoleEnum, User
from tests.conftest import login, make_person, make_user


def _users(db):
    chair_p = make_person(db, "Председатель Пётр", is_chairman=True)
    board_p = make_person(db, "Технарь Тимофей", is_board_member=True)
    other_p = make_person(db, "Другой Член Правления", is_board_member=True)
    chair = make_user(db, "chair", "pw", RoleEnum.CHAIRMAN, person=chair_p)
    board = make_user(db, "tech", "pw", RoleEnum.BOARD, person=board_p)
    other = make_user(db, "other", "pw", RoleEnum.BOARD, person=other_p)
    member = make_user(db, "member", "pw", RoleEnum.MEMBER)
    db.commit()
    return chair, board, other, member


def _as(client, username):
    client.get("/auth/logout")
    login(client, username, "pw")


def test_board_without_grant_has_no_access(client, db):
    _users(db)
    _as(client, "tech")
    resp = client.get("/max/", follow_redirects=True)
    assert "Недостаточно прав" in resp.get_data(as_text=True)
    html = client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "СМС и мессенджеры" not in html and "Учётные записи" not in html


def test_chairman_entrusts_messengers(client, db):
    _chair, board, *_ = _users(db)
    _as(client, "chair")
    client.post(f"/persons/accounts/{board.id}/grants", data={"grant_messengers": "1"})
    db.refresh(board)
    assert board.grants == "messengers"
    _as(client, "tech")
    assert client.get("/max/").status_code == 200
    assert client.get("/telegram/").status_code == 200
    html = client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "СМС и мессенджеры" in html
    # Другие разделы — нет.
    assert "Недостаточно прав" in client.get("/persons/accounts", follow_redirects=True).get_data(as_text=True)


def test_only_chairman_grants_and_only_to_board(client, db):
    _chair, board, other, member = _users(db)
    _as(client, "tech")
    client.post(f"/persons/accounts/{other.id}/grants", data={"grant_messengers": "1"})
    db.refresh(other)
    assert other.grants is None
    _as(client, "chair")
    client.post(f"/persons/accounts/{member.id}/grants", data={"grant_messengers": "1"})
    db.refresh(member)
    assert member.grants is None


def test_grant_ignored_after_role_drops(client, db):
    _chair, board, *_ = _users(db)
    board.grants = "messengers"
    board.role = RoleEnum.MEMBER
    db.commit()
    _as(client, "tech")
    assert "Недостаточно прав" in client.get("/max/", follow_redirects=True).get_data(as_text=True)


def test_accounts_delegate_cannot_escalate(client, db):
    chair, board, other, _member = _users(db)
    board.grants = "accounts_bank"
    spare = make_user(db, "spare", "pw", RoleEnum.MEMBER)
    db.commit()
    _as(client, "tech")
    assert client.get("/persons/accounts").status_code == 200
    chair_person = db.query(Person).filter_by(full_name="Председатель Пётр").one()
    # Нельзя: отвязать/отключить председателя, привязать запись к карточке председателя, свою запись.
    client.post(f"/persons/accounts/{chair.id}/unlink")
    client.post(f"/persons/accounts/{chair.id}/toggle-active")
    db.refresh(chair)
    assert chair.person_id == chair_person.id and chair.is_active
    chair.person_id = None
    db.commit()
    client.post(f"/persons/accounts/{spare.id}/link", data={"person_id": chair_person.id})
    db.refresh(spare)
    assert spare.person_id is None and spare.role == RoleEnum.MEMBER
    client.post(f"/persons/accounts/{board.id}/unlink")
    db.refresh(board)
    assert board.person_id is not None
    # Можно: обычный член правления.
    client.post(f"/persons/accounts/{other.id}/toggle-active")
    db.refresh(other)
    assert other.is_active is False


def test_board_cannot_reset_chairman_password(client, db):
    chair, *_ = _users(db)
    old_hash = chair.password_hash
    _as(client, "tech")
    html = client.post(f"/persons/{chair.person_id}/account/reset-password", follow_redirects=True).get_data(as_text=True)
    db.refresh(chair)
    assert chair.password_hash == old_hash
    assert "меняет только председатель" in html
