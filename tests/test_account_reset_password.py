"""Сброс пароля правлением с карточки человека (persons.reset_password):
пустое поле — случайный пароль (на странице его генерирует JS и шлёт
generated=1, без JS — сервер), такой пароль нужно сменить при входе."""
import re

from werkzeug.security import check_password_hash

from app.models import RoleEnum, User
from tests.conftest import make_person, make_user, login


def _setup(db, client):
    person = make_person(db, full_name="Some Member")
    make_user(db, "targetuser", "oldpass123", role=RoleEnum.MEMBER, person=person)
    chairman = make_person(db, full_name="Chairman")
    make_user(db, "chairuser", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman)
    db.commit()
    login(client, "chairuser", "pass1234")
    return person


def _user(db):
    db.expire_all()
    return db.query(User).filter_by(username="targetuser").one()


def test_explicit_password_kept_and_no_forced_change(db, client):
    person = _setup(db, client)
    resp = client.post(f"/persons/{person.id}/account/reset-password",
                       data={"password": "newpass456"}, follow_redirects=True)
    assert "Пароль обновлён." in resp.get_data(as_text=True)
    user = _user(db)
    assert check_password_hash(user.password_hash, "newpass456")
    assert not user.must_change_password


def test_empty_password_generated_on_server(db, client):
    person = _setup(db, client)
    resp = client.post(f"/persons/{person.id}/account/reset-password",
                       data={"password": ""}, follow_redirects=True)
    m = re.search(r"Новый пароль: (\w+)\.", resp.get_data(as_text=True))
    assert m
    user = _user(db)
    assert check_password_hash(user.password_hash, m.group(1))
    assert user.must_change_password


def test_client_generated_password_marked_copied(db, client):
    person = _setup(db, client)
    resp = client.post(f"/persons/{person.id}/account/reset-password",
                       data={"password": "Rnd7xKp2Qa", "generated": "1"}, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Новый пароль: Rnd7xKp2Qa" in html
    assert "скопирован в буфер обмена" in html
    user = _user(db)
    assert check_password_hash(user.password_hash, "Rnd7xKp2Qa")
    assert user.must_change_password


def test_detail_form_allows_empty_password(db, client):
    person = _setup(db, client)
    html = client.get(f"/persons/{person.id}").get_data(as_text=True)
    form = re.search(r'<form[^>]*reset-password[^>]*>.*?</form>', html, re.S).group(0)
    assert "data-random-password" in form
    assert "required" not in form
