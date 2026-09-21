"""
Дата рождения на карточке члена кооператива (Person.birth_date) — тот же
приём, что и у остальных личных данных: правление может внести напрямую
(persons.edit), сам член кооператива — только через конвейер ревизий
(PersonDataRevision, см. app/persons.py: _REVISION_FIELDS/_apply_revision,
app/cabinet.py: profile), с одобрением председателем.
"""
import datetime as dt
import json

from app.models import RoleEnum, Person, PersonDataRevision, PersonDataRevisionStatus

from tests.conftest import make_person, make_user, login


def test_board_edit_form_saves_birth_date(db, client):
    person = make_person(db, full_name="Сидоров Сидор Сидорович")
    make_user(db, "board210", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board210", "pass12345")

    resp = client.post(f"/persons/{person.id}/edit", data={
        "full_name": person.full_name, "birth_date": "1985-06-15",
    })
    assert resp.status_code == 302

    db.refresh(person)
    assert person.birth_date == dt.date(1985, 6, 15)


def test_person_detail_page_shows_birth_date(db, client):
    person = make_person(db, full_name="Петров Пётр Петрович", birth_date=dt.date(1990, 1, 2))
    make_user(db, "board211", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board211", "pass12345")

    resp = client.get(f"/persons/{person.id}")
    body = resp.get_data(as_text=True)
    assert "02.01.1990" in body


def test_cabinet_profile_submits_birth_date_as_revision(db, client):
    person = make_person(db, full_name="Кузнецов Кузьма Кузьмич")
    make_user(db, "member210", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member210", "pass12345")

    resp = client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "", "max": "",
        "registration_address": "", "residence_address": "",
        "birth_date": "1988-03-20",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
    })
    assert resp.status_code == 302

    revision = db.query(PersonDataRevision).filter_by(person_id=person.id).one()
    snap = json.loads(revision.fields_snapshot)
    assert snap["birth_date"] == "1988-03-20"
    # заявка ещё не применена к самой карточке — только после одобрения председателем
    db.refresh(person)
    assert person.birth_date is None


def test_approving_revision_applies_birth_date_to_person(db, client):
    person = make_person(db, full_name="Николаев Николай Николаевич")
    make_user(db, "member211", "pass12345", role=RoleEnum.MEMBER, person=person)
    make_user(db, "chair210", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "member211", "pass12345")
    client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "", "max": "",
        "registration_address": "", "residence_address": "",
        "birth_date": "1975-12-31",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
    })
    client.get("/auth/logout")

    revision = db.query(PersonDataRevision).filter_by(person_id=person.id, status=PersonDataRevisionStatus.PENDING).one()

    login(client, "chair210", "pass12345")
    resp = client.post(f"/persons/persons/{person.id}/revisions/approve/{revision.id}")
    assert resp.status_code == 302

    db.refresh(person)
    assert person.birth_date == dt.date(1975, 12, 31)


def test_cabinet_profile_shows_pending_birth_date(db, client):
    """Пока ревизия ждёт одобрения, форма показывает ПРЕДЛОЖЕННУЮ дату
    рождения, а не текущую (см. cabinet.profile: display_person)."""
    person = make_person(db, full_name="Смирнов Семён Семёнович", birth_date=dt.date(1960, 1, 1))
    make_user(db, "member212", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member212", "pass12345")

    client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "", "max": "",
        "registration_address": "", "residence_address": "",
        "birth_date": "1961-02-02",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
    })

    resp = client.get("/cabinet/profile")
    body = resp.get_data(as_text=True)
    assert 'value="1961-02-02"' in body
