"""
Заявления членов кооператива в правление: подача на бланке (только
собственник и только по своему гаражу), санитизация HTML редактора,
реквизиты заявителя на момент подачи, отметка «ознакомлен», ответ/отклонение
правлением с уведомлением заявителя, видимость на карточках гаража/человека.
"""
import datetime as dt

from app import notifications
from app.applications import sanitize_html
from app.models import (
    Application, ApplicationKind, ApplicationRead, ApplicationStatus, AuditLog, Phone, RoleEnum,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _owner(db, username="owner", number="42"):
    person = make_person(
        db, full_name="Петров Пётр Петрович", residence_address="г. Город, ул. Улица, 1",
        passport_series="1234", passport_number="567890", passport_issue_date=dt.date(2015, 5, 6),
    )
    db.add(Phone(person_id=person.id, number="+79001234567"))
    garage = make_garage(db, number=number)
    make_ownership(db, garage, person)
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    return person, garage


def _board(db, username="board1"):
    person = make_person(db, full_name="Сидоров Сидор Сидорович")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()
    return person


def _submit(client, garage, body="<p>Прошу заменить счётчик.</p>", kind="other", title="о замене счётчика"):
    return client.post("/applications/new", data={
        "kind": kind, "garage_id": str(garage.id), "title": title, "body_html": body,
    })


def test_owner_submits_application_with_snapshot(db, client):
    person, garage = _owner(db)
    login(client, "owner", "pass1234")

    form = client.get(f"/applications/new?garage_id={garage.id}").get_data(as_text=True)
    assert 'contenteditable="true"' in form
    assert "Петрова Петра Петровича" in form  # «от кого» — в родительном падеже

    resp = _submit(client, garage)
    assert resp.status_code == 302

    application = db.query(Application).one()
    assert application.number == f"{dt.date.today().year}-1"
    assert application.kind == ApplicationKind.OTHER
    assert application.status == ApplicationStatus.SUBMITTED
    assert application.person_id == person.id and application.garage_id == garage.id
    assert application.garage_number == "42"
    assert application.applicant_address == "г. Город, ул. Улица, 1"
    assert "1234" in application.applicant_passport and "06.05.2015" in application.applicant_passport
    assert application.applicant_phone == "+79001234567"
    assert db.query(AuditLog).filter_by(action="application.submit").count() == 1

    # Реквизиты — на момент подачи: правка профиля потом шапку не меняет.
    person.residence_address = "Новый адрес"
    db.commit()
    page = client.get(f"/applications/{application.id}").get_data(as_text=True)
    assert "г. Город, ул. Улица, 1" in page
    assert "Прошу заменить счётчик." in page


def test_numbers_increase_within_year(db, client):
    _person, garage = _owner(db)
    login(client, "owner", "pass1234")
    _submit(client, garage)
    _submit(client, garage)
    year = dt.date.today().year
    assert sorted(a.number for a in db.query(Application)) == [f"{year}-1", f"{year}-2"]


def test_non_owner_cannot_submit(db, client):
    person = make_person(db, full_name="Без Гаража")
    make_user(db, "noowner", "pass1234", person=person)
    other_garage = make_garage(db, number="7")
    db.commit()
    login(client, "noowner", "pass1234")

    assert client.get("/applications/new").status_code == 302
    _submit(client, other_garage)
    assert db.query(Application).count() == 0


def test_cannot_submit_for_someone_elses_garage(db, client):
    _owner(db)
    stranger_garage = make_garage(db, number="99")
    db.commit()
    login(client, "owner", "pass1234")

    resp = _submit(client, stranger_garage)
    assert resp.status_code == 200  # форма с ошибкой
    assert db.query(Application).count() == 0


def test_empty_body_rejected(db, client):
    _person, garage = _owner(db)
    login(client, "owner", "pass1234")
    resp = _submit(client, garage, body="<p>&nbsp;</p><p><br></p>")
    assert resp.status_code == 200
    assert db.query(Application).count() == 0


def test_sanitize_keeps_formatting_and_strips_dangerous():
    html = sanitize_html(
        '<p style="text-align: center; position: fixed">Текст</p>'
        '<span style="font-family: Arial; font-size: 16pt; background-image: url(x)">шрифт</span>'
        '<script>alert(1)</script><img src=x onerror=alert(1)><a href="javascript:x">ссылка</a>'
        '<font face="Arial">старый тег</font>'
    )
    assert "text-align: center" in html and "position" not in html
    assert "font-family: Arial" in html and "font-size: 16pt" in html and "background-image" not in html
    assert "<script" not in html and "<img" not in html and "<a" not in html and "<font" not in html
    assert "старый тег" in html and "ссылка" in html


def test_board_read_mark_and_visibility(db, client):
    _person, garage = _owner(db)
    board = _board(db)
    neighbour = make_person(db, full_name="Сосед Совладелец")
    make_ownership(db, garage, neighbour, share="0.5")
    make_user(db, "neighbour", "pass1234", person=neighbour)
    db.commit()

    login(client, "owner", "pass1234")
    _submit(client, garage)
    application = db.query(Application).one()
    client.get("/auth/logout")

    # Совладелец гаража чужое заявление не видит — ни по ссылке, ни на карточке гаража.
    login(client, "neighbour", "pass1234")
    assert client.get(f"/applications/{application.id}").status_code == 403
    assert f"/applications/{application.id}" not in client.get(f"/garages/{garage.id}").get_data(as_text=True)
    client.get("/auth/logout")

    login(client, "board1", "pass1234")
    page = client.get("/applications/").get_data(as_text=True)
    assert "новое" in page
    client.get(f"/applications/{application.id}")
    client.get(f"/applications/{application.id}")
    assert db.query(ApplicationRead).filter_by(application_id=application.id, person_id=board.id).count() == 1
    assert f"/applications/{application.id}" in client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert f"/applications/{application.id}" in client.get(f"/persons/{application.person_id}").get_data(as_text=True)


def test_board_answer_notifies_applicant(db, client, monkeypatch):
    person, garage = _owner(db)
    board = _board(db)
    login(client, "owner", "pass1234")
    _submit(client, garage)
    application = db.query(Application).one()
    client.get("/auth/logout")

    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body: sent.append((user.person_id, event, subject)))

    login(client, "board1", "pass1234")
    resp = client.post(f"/applications/{application.id}/decide", data={"decision": "answer", "response_html": "<p><br></p>"})
    assert resp.status_code == 302
    db.refresh(application)
    assert application.status == ApplicationStatus.SUBMITTED  # пустой ответ не принят

    client.post(f"/applications/{application.id}/decide", data={
        "decision": "answer", "response_html": "<p>Счётчик заменим в субботу.</p><script>x</script>",
    })
    db.refresh(application)
    assert application.status == ApplicationStatus.ANSWERED
    assert application.decided_by_person_id == board.id
    assert "<script" not in application.response_html
    assert (person.id, "my_requests", f"Ответ правления на заявление № {application.number}") in sent

    # Повторное решение не перезаписывает первое.
    client.post(f"/applications/{application.id}/decide", data={"decision": "reject"})
    db.refresh(application)
    assert application.status == ApplicationStatus.ANSWERED

    client.get("/auth/logout")
    login(client, "owner", "pass1234")
    page = client.get(f"/applications/{application.id}").get_data(as_text=True)
    assert "Счётчик заменим в субботу." in page
    assert "Ответ на заявление" in page


def test_board_reject_without_reason(db, client):
    _person, garage = _owner(db)
    _board(db)
    login(client, "owner", "pass1234")
    _submit(client, garage)
    application = db.query(Application).one()
    client.get("/auth/logout")

    login(client, "board1", "pass1234")
    client.post(f"/applications/{application.id}/decide", data={"decision": "reject", "response_html": ""})
    db.refresh(application)
    assert application.status == ApplicationStatus.REJECTED
    assert application.response_html is None
    assert "Правление рассмотрело Ваше заявление и отклонило его." in client.get(
        f"/applications/{application.id}").get_data(as_text=True)


def test_member_cannot_decide(db, client):
    _person, garage = _owner(db)
    login(client, "owner", "pass1234")
    _submit(client, garage)
    application = db.query(Application).one()
    client.post(f"/applications/{application.id}/decide", data={"decision": "answer", "response_html": "<p>Сам себе</p>"})
    db.refresh(application)
    assert application.status == ApplicationStatus.SUBMITTED


def test_pdf(db, client):
    _person, garage = _owner(db)
    login(client, "owner", "pass1234")
    _submit(client, garage)
    application = db.query(Application).one()
    resp = client.get(f"/applications/{application.id}/pdf")
    assert resp.status_code == 200
    assert resp.data[:4] == b"%PDF"
