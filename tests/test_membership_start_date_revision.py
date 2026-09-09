"""
Дата начала членства (Person.membership_start_date) теперь редактируется
самим членом кооператива через /cabinet/profile — тем же конвейером
ревизий (PersonDataRevision), что и остальные контакты/паспорт (см.
app/persons.py: _REVISION_FIELDS/_apply_revision, app/cabinet.py: profile).
Раньше поле было только просмотром в «Официальных данных».
"""
import json

from app.models import RoleEnum, PersonDataRevision, PersonDataRevisionStatus

from tests.conftest import make_person, make_user, login


def test_cabinet_profile_submits_membership_start_date_as_revision(db, client):
    person = make_person(db, full_name="Членов Член Членович")
    make_user(db, "member300", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "member300", "pass12345")

    resp = client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "", "max": "",
        "registration_address": "", "residence_address": "",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
        "membership_start_date": "2020-05-01",
    })
    assert resp.status_code == 302

    revision = db.query(PersonDataRevision).filter_by(person_id=person.id).one()
    snap = json.loads(revision.fields_snapshot)
    assert snap["membership_start_date"] == "2020-05-01"
    # заявка ещё не применена — только после одобрения председателем
    db.refresh(person)
    assert person.membership_start_date is None


def test_approving_revision_applies_membership_start_date(db, client):
    person = make_person(db, full_name="Уставов Устав Уставович")
    make_user(db, "member301", "pass12345", role=RoleEnum.MEMBER, person=person)
    make_user(db, "chair300", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()

    login(client, "member301", "pass12345")
    client.post("/cabinet/profile", data={
        "phones": "", "email": "", "vk": "", "max": "",
        "registration_address": "", "residence_address": "",
        "passport_series": "", "passport_number": "", "passport_issue_date": "",
        "membership_start_date": "2019-11-15",
    })
    client.get("/auth/logout")

    revision = db.query(PersonDataRevision).filter_by(person_id=person.id, status=PersonDataRevisionStatus.PENDING).one()

    login(client, "chair300", "pass12345")
    resp = client.post(f"/persons/persons/{person.id}/revisions/approve/{revision.id}")
    assert resp.status_code == 302

    db.refresh(person)
    assert person.membership_start_date.isoformat() == "2019-11-15"
