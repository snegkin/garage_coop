"""
Порядок запросов на изменение данных карточки (app/person_revisions.py):
новый запрос заменяет ожидающие, устаревший одобрить нельзя, последнее
одобрение председатель может отменить.
"""
import datetime as dt
import json

from app.models import Phone, PersonDataRevision, PersonDataRevisionStatus as S, RoleEnum

from tests.conftest import login, make_person, make_user

EMPTY_FORM = {
    "phones": "", "email": "", "telegram": "", "vk": "", "max": "",
    "registration_address": "", "residence_address": "",
    "passport_series": "", "passport_number": "", "passport_issue_date": "",
    "membership_start_date": "",
}


def _setup(db):
    person = make_person(db, full_name="Калинов Калина", email="old@example.com")
    db.add(Phone(person_id=person.id, number="+7 900 000-00-01"))
    make_user(db, "member", "pw", person=person)
    make_user(db, "chair", "pw", role=RoleEnum.CHAIRMAN)
    db.commit()
    return person


def _submit(client, **fields):
    client.post("/cabinet/profile", data={**EMPTY_FORM, **fields})


def _revisions(db, person):
    db.expire_all()
    return db.query(PersonDataRevision).filter_by(person_id=person.id).order_by(PersonDataRevision.id).all()


def _url(person, action, revision):
    return f"/persons/persons/{person.id}/revisions/{action}/{revision.id}"


def test_new_request_supersedes_pending(db, client):
    person = _setup(db)
    login(client, "member", "pw")
    _submit(client, email="a@example.com", phones="+7 900 000-00-01")
    _submit(client, email="b@example.com", phones="+7 900 000-00-01, +7 900 000-00-02")
    first, second = _revisions(db, person)
    assert (first.status, second.status) == (S.SUPERSEDED, S.PENDING)


def test_outdated_request_cannot_be_approved(db, client):
    """Случай с двумя карточками: председатель одобряет оба по порядку
    показа (сначала новый, потом старый) — старый не должен затереть новый."""
    person = _setup(db)
    old = PersonDataRevision(
        person_id=person.id, submitted_by_user_id=1, status=S.PENDING,
        fields_snapshot=json.dumps({"email": "short@example.com", "phones": []}),
        submitted_at=dt.datetime(2026, 11, 1, 10, 0),
    )
    new = PersonDataRevision(
        person_id=person.id, submitted_by_user_id=1, status=S.PENDING,
        fields_snapshot=json.dumps({"email": "full@example.com", "phones": ["+7 900 000-00-01"],
                                    "registration_address": "ул. Полная, 1"}),
        submitted_at=dt.datetime(2026, 11, 1, 11, 0),
    )
    db.add_all([old, new])
    db.commit()
    login(client, "chair", "pw")
    client.post(_url(person, "approve", new))
    client.post(_url(person, "approve", old))
    db.expire_all()
    assert person.email == "full@example.com"
    assert person.registration_address == "ул. Полная, 1"
    assert db.get(PersonDataRevision, old.id).status == S.SUPERSEDED


def test_approving_older_while_newer_pending_is_refused(db, client):
    person = _setup(db)
    old = PersonDataRevision(person_id=person.id, submitted_by_user_id=1, status=S.PENDING,
                             fields_snapshot=json.dumps({"email": "x@example.com"}),
                             submitted_at=dt.datetime(2026, 11, 1, 10, 0))
    new = PersonDataRevision(person_id=person.id, submitted_by_user_id=1, status=S.PENDING,
                             fields_snapshot=json.dumps({"email": "y@example.com"}),
                             submitted_at=dt.datetime(2026, 11, 1, 11, 0))
    db.add_all([old, new])
    db.commit()
    login(client, "chair", "pw")
    client.post(_url(person, "approve", old))
    db.expire_all()
    assert person.email == "old@example.com"
    assert db.get(PersonDataRevision, old.id).status == S.SUPERSEDED
    assert db.get(PersonDataRevision, new.id).status == S.PENDING


def test_revert_restores_card_before_approval(db, client):
    person = _setup(db)
    login(client, "member", "pw")
    _submit(client, email="new@example.com", phones="+7 900 000-00-09", registration_address="Новый адрес")
    client.get("/auth/logout")
    [revision] = _revisions(db, person)
    login(client, "chair", "pw")
    client.post(_url(person, "approve", revision))
    db.expire_all()
    assert person.email == "new@example.com"

    html = client.get(f"/persons/{person.id}").get_data(as_text=True)
    assert _url(person, "revert", revision) in html

    client.post(_url(person, "revert", revision))
    db.expire_all()
    assert person.email == "old@example.com"
    assert person.registration_address is None
    assert [p.number for p in person.phones] == ["+7 900 000-00-01"]
    reverted = db.get(PersonDataRevision, revision.id)
    assert reverted.status == S.REVERTED and reverted.reverted_by_user_id is not None


def test_only_latest_approval_can_be_reverted(db, client):
    person = _setup(db)
    login(client, "chair", "pw")
    for i, email in enumerate(["one@example.com", "two@example.com"]):
        rev = PersonDataRevision(person_id=person.id, submitted_by_user_id=1, status=S.PENDING,
                                 fields_snapshot=json.dumps({"email": email}),
                                 submitted_at=dt.datetime(2026, 11, 1, 10 + i, 0))
        db.add(rev)
        db.commit()
        client.post(_url(person, "approve", rev))
    first, second = _revisions(db, person)
    client.post(_url(person, "revert", first))
    db.expire_all()
    assert db.get(PersonDataRevision, first.id).status == S.APPROVED
    assert person.email == "two@example.com"


def test_revert_of_legacy_approval_falls_back_to_previous_approved(db, client):
    """Одобренные до появления снимка (applied_over_snapshot = NULL): как у
    Калинина — оба одобрены, последним старый. Отмена последнего возвращает
    данные предыдущего одобренного запроса."""
    person = _setup(db)
    newer = PersonDataRevision(
        person_id=person.id, submitted_by_user_id=1, status=S.APPROVED,
        fields_snapshot=json.dumps({"email": "full@example.com", "phones": ["+7 900 000-00-01"],
                                    "registration_address": "ул. Полная, 1"}),
        submitted_at=dt.datetime(2026, 11, 1, 11, 0), reviewed_at=dt.datetime(2026, 11, 2, 9, 0),
    )
    older = PersonDataRevision(
        person_id=person.id, submitted_by_user_id=1, status=S.APPROVED,
        fields_snapshot=json.dumps({"email": "short@example.com", "phones": []}),
        submitted_at=dt.datetime(2026, 11, 1, 10, 0), reviewed_at=dt.datetime(2026, 11, 2, 9, 1),
    )
    person.email = "short@example.com"
    db.add_all([newer, older])
    db.commit()
    login(client, "chair", "pw")
    client.post(_url(person, "revert", older))
    db.expire_all()
    assert person.email == "full@example.com"
    assert person.registration_address == "ул. Полная, 1"
    assert db.get(PersonDataRevision, older.id).status == S.REVERTED


def test_history_lists_oldest_first(db, client):
    person = _setup(db)
    for i, email in enumerate(["first@example.com", "second@example.com"]):
        db.add(PersonDataRevision(person_id=person.id, submitted_by_user_id=1, status=S.REJECTED,
                                  fields_snapshot=json.dumps({"email": email}),
                                  submitted_at=dt.datetime(2026, 11, 1, 10 + i, 0)))
    db.commit()
    login(client, "chair", "pw")
    html = client.get(f"/persons/{person.id}").get_data(as_text=True)
    assert html.index("first@example.com") < html.index("second@example.com")
