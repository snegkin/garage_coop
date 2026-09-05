"""
Массовое создание учётных записей в мастере первоначальной настройки
(setup_wizard.accounts_step/accounts_create) — логин по умолчанию первая
буква имени + фамилия (см. _generate_login), коллизии не создаются
автоматически, а показываются человеку списком для правки. Люди с уже
существующей учётной записью не попадают в список вовсе — их нельзя
случайно пересоздать.
"""
from app.models import RoleEnum, User

from tests.conftest import make_person, make_user, login


def _make_chairman(db, username="chair900"):
    person = make_person(db, full_name="Председателев Пред Предович")
    make_user(db, username, "pass12345", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()
    return person


def test_generate_login_formula(db, client):
    _make_chairman(db)
    make_person(db, full_name="Иванов Иван Иванович")
    db.commit()
    login(client, "chair900", "pass12345")

    resp = client.get("/setup/accounts")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="ииванов"' in body


def test_person_with_existing_account_excluded_from_list(db, client):
    _make_chairman(db)
    person = make_person(db, full_name="Петров Пётр Петрович")
    make_user(db, "petrov1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "chair900", "pass12345")

    resp = client.get("/setup/accounts")
    body = resp.get_data(as_text=True)
    assert "Петров Пётр Петрович" not in body


def test_collision_between_two_people_is_flagged(db, client):
    _make_chairman(db)
    make_person(db, full_name="Петров Пётр Петрович")
    make_person(db, full_name="Петров Павел Петрович")
    db.commit()
    login(client, "chair900", "pass12345")

    resp = client.get("/setup/accounts")
    body = resp.get_data(as_text=True)
    assert "is-invalid" in body


def test_create_accounts_for_selected_people(db, client):
    chairman_person = _make_chairman(db)
    p1 = make_person(db, full_name="Сидоров Семён Семёнович")
    p2 = make_person(db, full_name="Кузнецов Кузьма Кузьмич")
    db.commit()
    login(client, "chair900", "pass12345")

    resp = client.post("/setup/accounts/create", data={
        "person_id": [str(p1.id)],
        f"login_{p1.id}": "ссидоров",
        f"login_{p2.id}": "ккузнецов",
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ссидоров" in body

    user = db.query(User).filter_by(person_id=p1.id).one()
    assert user.must_change_password is True
    assert user.role == RoleEnum.MEMBER
    assert db.query(User).filter_by(person_id=p2.id).count() == 0


def test_create_accounts_uses_person_role_flags(db, client):
    _make_chairman(db)
    board_member = make_person(db, full_name="Правленцев Прав Правленцевич", is_board_member=True)
    db.commit()
    login(client, "chair900", "pass12345")

    client.post("/setup/accounts/create", data={
        "person_id": [str(board_member.id)],
        f"login_{board_member.id}": "пправленцев",
    })

    user = db.query(User).filter_by(person_id=board_member.id).one()
    assert user.role == RoleEnum.BOARD


def test_create_rejects_when_selected_login_collides(db, client):
    _make_chairman(db)
    p1 = make_person(db, full_name="Волков Влад Владович")
    p2 = make_person(db, full_name="Волков Виктор Викторович")
    db.commit()
    login(client, "chair900", "pass12345")

    resp = client.post("/setup/accounts/create", data={
        "person_id": [str(p1.id), str(p2.id)],
        f"login_{p1.id}": "ввволков",
        f"login_{p2.id}": "ввволков",
    })
    assert resp.status_code == 200
    assert db.query(User).filter_by(person_id=p1.id).count() == 0
    assert db.query(User).filter_by(person_id=p2.id).count() == 0


def test_create_rejects_login_already_taken(db, client):
    _make_chairman(db)
    taken_person = make_person(db, full_name="Занятов Занят Занятович")
    make_user(db, "existing_login", "pass12345", role=RoleEnum.MEMBER, person=taken_person)
    new_person = make_person(db, full_name="Новиков Ной Ноевич")
    db.commit()
    login(client, "chair900", "pass12345")

    resp = client.post("/setup/accounts/create", data={
        "person_id": [str(new_person.id)],
        f"login_{new_person.id}": "existing_login",
    })
    assert resp.status_code == 200
    assert db.query(User).filter_by(person_id=new_person.id).count() == 0


def test_board_role_cannot_access_accounts_step(db, client):
    """roles_required(CHAIRMAN) не абортит 403 — редиректит с flash-сообщением
    (см. auth.roles_required), это касается всех шагов мастера, не только
    этого — правление (не председатель) на /setup/accounts не попадает."""
    person = make_person(db, full_name="Правление Один Одинович")
    make_user(db, "board910", "pass12345", role=RoleEnum.BOARD, person=person)
    db.commit()
    login(client, "board910", "pass12345")

    resp = client.get("/setup/accounts")
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/setup/accounts"


def test_forced_password_change_blocks_navigation_until_changed(db, client):
    person = make_person(db, full_name="Новичков Новичок Новичкович")
    make_user(db, "newmember1", "TempPass123", role=RoleEnum.MEMBER, person=person)
    user = db.query(User).filter_by(username="newmember1").one()
    user.must_change_password = True
    db.commit()

    login(client, "newmember1", "TempPass123")

    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert "/auth/change-password" in resp.headers["Location"]

    resp2 = client.post("/auth/change-password", data={
        "new_password": "NewSecret456", "confirm_password": "NewSecret456",
    })
    assert resp2.status_code == 302

    db.refresh(user)
    assert user.must_change_password is False

    # /dashboard сам по себе редиректит рядового члена на /cabinet/garages
    # (main.dashboard: not is_board()) — это не связано с нашей фичей,
    # проверяем только что мы больше НЕ редиректим на смену пароля.
    resp3 = client.get("/dashboard")
    assert resp3.status_code == 302
    assert "/auth/change-password" not in resp3.headers["Location"]
