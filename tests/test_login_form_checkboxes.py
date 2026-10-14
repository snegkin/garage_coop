"""
Галочки формы входа (auth/_login_form.html, auth._complete_login):
«Разрешить уведомления» — отмечена по умолчанию, push в этом браузере (тот
же одноразовый флаг webpush_prompt, что и у принудительной смены пароля,
см. tests/test_webpush_login_prompt.py); обработка персональных данных —
неактивная и всегда отмеченная: основание — устав (п. 5 ч. 1 ст. 6 152-ФЗ),
согласие не требуется, вход от неё не зависит.
"""
from app.models import AuditLog, NotificationChannel, Phone, User

from tests.conftest import make_person, make_user


def _post_login(client, username="member1", password="pass12345", **extra):
    return client.post("/auth/login", data={"username": username, "password": password, **extra})


def _logged_in(client) -> bool:
    with client.session_transaction() as sess:
        return "user_id" in sess


def _tag(html: str, marker: str) -> str:
    start = html.index(marker)
    return html[html.rindex("<", 0, start):html.index(">", start)]


def test_login_does_not_require_pd_checkbox_and_records_notice_once(app, db, client):
    make_user(db, "member1", "pass12345")
    db.commit()

    _post_login(client)
    assert _logged_in(client)
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().pd_notice_at is not None

    client.get("/auth/logout")
    _post_login(client)
    assert db.query(AuditLog).filter_by(action="account.pd_notice").count() == 1


def test_phone_registration_does_not_require_pd_checkbox(app, db, client, monkeypatch):
    from app import auth
    sent = []

    class FakeSms:
        def send(self, phone, text):
            sent.append(phone)

    monkeypatch.setattr(auth, "get_sms_client", lambda: FakeSms())
    person = make_person(db, full_name="Новиков Новик Новикович")
    db.add(Phone(person_id=person.id, number="+7 900 555-44-33"))
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9005554433", "password": "mypassword1", "enable_push": "1"})
    assert resp.status_code == 200
    assert len(sent) == 1
    assert b'name="enable_push" value="1"' in resp.data


def test_login_form_checkboxes(app, db, client):
    html = client.get("/auth/login").get_data(as_text=True)
    push = _tag(html, 'name="enable_push"')
    assert "checked" in push

    pd = _tag(html, 'id="pdProcessingUsername-page"')
    assert "checked" in pd and "disabled" in pd
    assert "name=" not in pd  # на сервер не отправляется — от неё ничего не зависит
    assert 'href="/personal-data"' in html


def test_enable_push_keeps_working_email_channel(app, db, client):
    person = make_person(db, full_name="Почтов Почт Почтович", email="mail@example.com")
    make_user(db, "member1", "pass12345", person=person)
    db.commit()

    _post_login(client, enable_push="1")
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel == NotificationChannel.EMAIL
    assert "/cabinet/profile/webpush/subscribe" in client.get("/").get_data(as_text=True)
    assert "/cabinet/profile/webpush/subscribe" not in client.get("/").get_data(as_text=True)  # флаг одноразовый


def test_enable_push_switches_channel_when_email_not_ready(app, db, client):
    person = make_person(db, full_name="Безпочтов Без Безович")
    make_user(db, "member1", "pass12345", person=person)
    db.commit()

    _post_login(client, enable_push="1")
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel == NotificationChannel.WEBPUSH


def test_unchecked_enable_push_changes_nothing(app, db, client):
    person = make_person(db, full_name="Безпочтов Без Безович")
    make_user(db, "member1", "pass12345", person=person)
    db.commit()

    _post_login(client)
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel == NotificationChannel.EMAIL
    assert "/cabinet/profile/webpush/subscribe" not in client.get("/").get_data(as_text=True)


def test_pd_processing_page_is_public(app, db, client):
    html = client.get("/personal-data").get_data(as_text=True)
    assert "Обработка персональных данных" in html
    assert "п. 5 ч. 1 ст. 6" in html
    assert "ч. 5 ст. 18" in html
    assert "1099–1101 Гражданского кодекса" in html
    assert "третьим лицам" not in html
    assert "даю согласие" not in html.lower()


def test_enable_push_respects_explicit_opt_out(app, db, client):
    """Галочка отмечена по умолчанию — выбор «Не получать уведомления» в
    профиле при каждом входе не отменяется."""
    person = make_person(db, full_name="Безпочтов Без Безович")
    user = make_user(db, "member1", "pass12345", person=person)
    user.notify_channel = None
    db.commit()

    _post_login(client, enable_push="1")
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel is None
