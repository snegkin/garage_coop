"""
Включение/отключение доступа учётной записи прямо с /persons/accounts
(app/persons.py:toggle_account_active) — по user_id, а не по person_id, как
у уже существовавшего persons.toggle_active. Это единственный способ
отключить/включить служебную учётную запись без привязанного человека
(например "chairman" из seed.py — своей карточки персоны у неё нет, значит
и persons.toggle_active для неё не сработает).
"""
from app.models import RoleEnum, User

from tests.conftest import make_person, make_user, login


def test_toggle_unlinked_account_active(app, db, client):
    """Служебная учётная запись без person_id (как chairman из seed.py) —
    единственная кнопка, которая может её отключить/включить, теперь есть
    на /persons/accounts."""
    chairman_person = make_person(db, full_name="Chairman Person")
    make_user(db, "chairman_actor", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman_person)
    unlinked = User(username="chairman", password_hash="x", role=RoleEnum.CHAIRMAN, is_active=True, person_id=None)
    db.add(unlinked)
    db.commit()

    login(client, "chairman_actor", "pass1234")
    resp = client.post(f"/persons/accounts/{unlinked.id}/toggle-active")
    assert resp.status_code == 302

    db.refresh(unlinked)
    assert unlinked.is_active is False

    resp = client.post(f"/persons/accounts/{unlinked.id}/toggle-active")
    assert resp.status_code == 302
    db.refresh(unlinked)
    assert unlinked.is_active is True


def test_toggle_account_active_requires_chairman(app, db, client):
    target_person = make_person(db, full_name="Target Member")
    target_user = make_user(db, "target1", "pass1234", role=RoleEnum.MEMBER, person=target_person)
    board_person = make_person(db, full_name="Board Only")
    make_user(db, "boardonly", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    login(client, "boardonly", "pass1234")
    resp = client.post(f"/persons/accounts/{target_user.id}/toggle-active")
    assert resp.status_code == 302
    db.refresh(target_user)
    assert target_user.is_active is True  # не изменилось — доступ запрещён


def test_accounts_page_shows_toggle_button_for_unlinked_account(app, db, client):
    make_user(db, "chairman_actor2", "pass1234", role=RoleEnum.CHAIRMAN)
    unlinked = User(username="chairman", password_hash="x", role=RoleEnum.CHAIRMAN, is_active=True, person_id=None)
    db.add(unlinked)
    db.commit()

    login(client, "chairman_actor2", "pass1234")
    resp = client.get("/persons/accounts")
    html = resp.get_data(as_text=True)
    assert f'/persons/accounts/{unlinked.id}/toggle-active' in html
