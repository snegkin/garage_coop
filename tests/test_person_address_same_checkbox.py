"""
Флажок «Совпадает с местом регистрации» на форме /persons/<id>/edit и в
личном кабинете (/cabinet/profile) — чисто клиентская фича (JS скрывает
поле «Адрес проживания» и синхронизирует его значение с «Адресом
регистрации», см. persons/_fields.html и cabinet/profile.html): сервер
как и раньше просто читает residence_address из формы. Здесь проверяется
только серверный рендеринг начального состояния чекбокса — что он
отмечен, когда сохранённые адреса совпадают, и не отмечен, когда различаются
или ещё не заполнены (JS-поведение проверялось вручную через Playwright).
"""
from app.models import RoleEnum

from tests.conftest import make_person, make_user, login


def _checkbox_is_checked(body: str) -> bool:
    tag = body.split('id="residence_same_as_registration"', 1)[1].split(">", 1)[0]
    return "checked" in tag


def test_edit_form_checkbox_checked_when_addresses_match(db, client):
    person = make_person(
        db, full_name="Совпадов Совпад Совпадович",
        registration_address="г. Москва, ул. Ленина, д. 1",
        residence_address="г. Москва, ул. Ленина, д. 1",
    )
    make_user(db, "board300", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board300", "pass12345")

    resp = client.get(f"/persons/{person.id}/edit")
    body = resp.get_data(as_text=True)
    assert _checkbox_is_checked(body)


def test_edit_form_checkbox_unchecked_when_addresses_differ(db, client):
    person = make_person(
        db, full_name="Разный Раз Разович",
        registration_address="г. Москва, ул. Ленина, д. 1",
        residence_address="г. Москва, ул. Мира, д. 5",
    )
    make_user(db, "board301", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board301", "pass12345")

    resp = client.get(f"/persons/{person.id}/edit")
    body = resp.get_data(as_text=True)
    assert not _checkbox_is_checked(body)


def test_edit_form_checkbox_unchecked_when_both_addresses_empty(db, client):
    person = make_person(db, full_name="Пустой Пуст Пустович")
    make_user(db, "board302", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board302", "pass12345")

    resp = client.get(f"/persons/{person.id}/edit")
    body = resp.get_data(as_text=True)
    assert not _checkbox_is_checked(body)


def test_cabinet_profile_checkbox_checked_when_addresses_match(db, client):
    person = make_person(
        db, full_name="Житель Жил Жилович",
        registration_address="г. Москва, ул. Ленина, д. 1",
        residence_address="г. Москва, ул. Ленина, д. 1",
    )
    make_user(db, "member300", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member300", "pass12345")

    resp = client.get("/cabinet/profile")
    body = resp.get_data(as_text=True)
    assert _checkbox_is_checked(body)
