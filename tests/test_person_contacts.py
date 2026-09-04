"""
Контакты VK/MAX на карточке члена кооператива (Person.vk/max_messenger) —
тот же приём, что и у email/Telegram: свободный текст, ссылки строятся
в app/contact_format.py (vk_link/max_link), поля идут через тот же
конвейер ревизий персональных данных (PersonDataRevision), что и остальные
контакты (см. app/persons.py: _REVISION_FIELDS/_apply_revision,
app/cabinet.py: profile).
"""
import json

from app.models import RoleEnum, Person, PersonDataRevision, PersonDataRevisionStatus

from tests.conftest import make_person, make_user, login


def test_board_edit_form_saves_vk_and_max(db, client):
    person = make_person(db, full_name="Сидоров Сидор Сидорович")
    make_user(db, "board200", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board200", "pass12345")

    resp = client.post(f"/persons/{person.id}/edit", data={
        "full_name": person.full_name, "vk": "sidorov", "max": "+79161234567",
    })
    assert resp.status_code == 302

    db.refresh(person)
    assert person.vk == "sidorov"
    assert person.max_messenger == "+79161234567"


def test_person_detail_page_shows_vk_and_max_links(db, client):
    person = make_person(db, full_name="Петров Пётр Петрович", vk="petrov", max_messenger="petrov")
    make_user(db, "board201", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board201", "pass12345")

    resp = client.get(f"/persons/{person.id}")
    body = resp.get_data(as_text=True)
    assert 'href="https://vk.com/petrov"' in body
    assert "<td>petrov</td>" in body  # MAX — просто текст, без придуманной ссылки


def test_cabinet_profile_submits_vk_and_max_as_revision(db, client):
    person = make_person(db, full_name="Кузнецов Кузьма Кузьмич")
    make_user(db, "member200", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member200", "pass12345")

    resp = client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "kuznetsov", "max": "kuznetsov_max",
        "registration_address": "", "residence_address": "",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
    })
    assert resp.status_code == 302

    revision = db.query(PersonDataRevision).filter_by(person_id=person.id).one()
    snap = json.loads(revision.fields_snapshot)
    assert snap["vk"] == "kuznetsov"
    assert snap["max_messenger"] == "kuznetsov_max"
    # заявка ещё не применена к самой карточке — только после одобрения председателем
    db.refresh(person)
    assert person.vk is None


def test_approving_revision_applies_vk_and_max_to_person(db, client):
    person = make_person(db, full_name="Николаев Николай Николаевич")
    make_user(db, "member201", "pass12345", role=RoleEnum.MEMBER, person=person)
    make_user(db, "chair200", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "member201", "pass12345")
    client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "nikolaev", "max": "",
        "registration_address": "", "residence_address": "",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
    })
    client.get("/auth/logout")

    revision = db.query(PersonDataRevision).filter_by(person_id=person.id, status=PersonDataRevisionStatus.PENDING).one()

    login(client, "chair200", "pass12345")
    resp = client.post(f"/persons/persons/{person.id}/revisions/approve/{revision.id}")
    assert resp.status_code == 302

    db.refresh(person)
    assert person.vk == "nikolaev"
