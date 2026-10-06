"""
Нестрогий вход по логину (auth.find_user_by_login, login_generation.login_key):
регистр и кириллица/латиница не важны — «СТарасов» входит как «starasov».
Обратная сторона — логины, совпадающие по ключу, заводить нельзя
(auth.login_taken), иначе вход стал бы неоднозначным.
"""
import pytest

from app.models import RoleEnum, User
from app.login_generation import login_key, generate_unique_login

from tests.conftest import make_person, make_user, login


@pytest.mark.parametrize("typed", ["starasov", "Starasov", "STarasov", "STARASOV", "СТарасов", "Старасов", "старасов", " starasov "])
def test_login_key_variants(typed):
    assert login_key(typed) == "starasov"


@pytest.mark.parametrize("typed", ["Starasov", "STarasov", "СТарасов", "Старасов", "старасов"])
def test_login_accepts_case_and_cyrillic_variants(db, client, typed):
    make_user(db, "starasov", "secret", role=RoleEnum.MEMBER)
    db.commit()

    resp = login(client, typed, "secret")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("user_id") == db.query(User).filter_by(username="starasov").one().id


def test_login_variant_still_checks_password(db, client):
    make_user(db, "starasov", "secret", role=RoleEnum.MEMBER)
    db.commit()

    resp = login(client, "Старасов", "wrong")
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_ambiguous_legacy_logins_require_exact_match(db, client):
    """Если по ключу совпали две старые учётные записи — нестрогий вход не
    угадывает, а точный логин по-прежнему работает."""
    make_user(db, "Ivanov", "one", role=RoleEnum.MEMBER)
    make_user(db, "ivanov", "two", role=RoleEnum.MEMBER)
    db.commit()

    login(client, "Иванов", "one")
    with client.session_transaction() as sess:
        assert "user_id" not in sess

    login(client, "Ivanov", "one")
    with client.session_transaction() as sess:
        assert sess.get("user_id") == db.query(User).filter_by(username="Ivanov").one().id


def test_generate_unique_login_treats_case_variants_as_taken():
    assert generate_unique_login("Иванов Иван", {"IIvanov"}) == "iivanov2"


def _board_client(db, client):
    board = make_person(db, full_name="Правлёв Пётр Петрович", is_board_member=True)
    make_user(db, "board", "pw", role=RoleEnum.BOARD, person=board)
    db.commit()
    login(client, "board", "pw")


def test_create_account_rejects_login_differing_only_by_case_or_layout(db, client):
    make_user(db, "starasov", "secret", role=RoleEnum.MEMBER)
    person = make_person(db, full_name="Старасов Сергей")
    _board_client(db, client)

    client.post(f"/persons/{person.id}/account/create", data={"username": "Старасов", "password": "x"})
    assert db.query(User).filter_by(person_id=person.id).first() is None


def test_change_username_rejects_key_collision_but_allows_own_case_change(db, client):
    make_user(db, "starasov", "secret", role=RoleEnum.MEMBER)
    person = make_person(db, full_name="Петров Пётр")
    user = make_user(db, "ppetrov", "pw", role=RoleEnum.MEMBER, person=person)
    _board_client(db, client)

    client.post(f"/persons/{person.id}/account/change-username", data={"username": "STarasov"})
    db.refresh(user)
    assert user.username == "ppetrov"

    client.post(f"/persons/{person.id}/account/change-username", data={"username": "PPetrov"})
    db.refresh(user)
    assert user.username == "PPetrov"
