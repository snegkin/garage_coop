"""
Галочки формы входа (auth/_login_form.html, auth._complete_login):
согласие на обработку персональных данных — обязательно, пока человек его
ни разу не давал (User.pd_consent_at), и «Разрешить уведомления» — push в
этом браузере (тот же одноразовый флаг webpush_prompt, что и у
принудительной смены пароля, см. tests/test_webpush_login_prompt.py).
"""
from app.models import AuditLog, NotificationChannel, User

from tests.conftest import make_person, make_user


def _post_login(client, username="member1", password="pass12345", **extra):
    return client.post("/auth/login", data={"username": username, "password": password, **extra})


def _logged_in(client) -> bool:
    with client.session_transaction() as sess:
        return "user_id" in sess


def test_first_login_without_consent_is_refused(app, db, client):
    make_user(db, "member1", "pass12345", pd_consent=False)
    db.commit()

    resp = _post_login(client)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/auth/login")
    assert not _logged_in(client)
    assert db.query(User).filter_by(username="member1").one().pd_consent_at is None
    assert db.query(AuditLog).filter_by(action="auth.login").count() == 0


def test_first_login_with_consent_records_it(app, db, client):
    make_user(db, "member1", "pass12345", pd_consent=False)
    db.commit()

    resp = _post_login(client, pd_consent="1")
    assert resp.status_code == 302
    assert _logged_in(client)
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().pd_consent_at is not None
    assert db.query(AuditLog).filter_by(action="account.pd_consent").count() == 1
    assert "pd_consent=1" in resp.headers.get("Set-Cookie", "")


def test_consent_not_required_again(app, db, client):
    make_user(db, "member1", "pass12345")  # согласие уже давал раньше
    db.commit()

    _post_login(client)
    assert _logged_in(client)
    assert db.query(AuditLog).filter_by(action="account.pd_consent").count() == 0


def test_phone_registration_without_consent_sends_no_sms(app, db, client, monkeypatch):
    from app import auth
    from app.models import Phone
    sent = []

    class FakeSms:
        def send(self, phone, text):
            sent.append(phone)

    monkeypatch.setattr(auth, "get_sms_client", lambda: FakeSms())
    person = make_person(db, full_name="Новиков Новик Новикович")
    db.add(Phone(person_id=person.id, number="+7 900 555-44-33"))
    db.commit()

    resp = client.post("/auth/login-phone", data={"phone": "9005554433", "password": "mypassword1"})
    assert resp.status_code == 302
    assert sent == []

    resp = client.post("/auth/login-phone", data={"phone": "9005554433", "password": "mypassword1", "pd_consent": "1"})
    assert resp.status_code == 200
    assert len(sent) == 1
    assert b'name="pd_consent" value="1"' in resp.data


def test_enable_push_keeps_working_email_channel(app, db, client):
    person = make_person(db, full_name="Почтов Почт Почтович", email="mail@example.com")
    make_user(db, "member1", "pass12345", person=person)
    db.commit()

    _post_login(client, enable_push="1")
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel == NotificationChannel.EMAIL
    page = client.get("/").get_data(as_text=True)
    assert "/cabinet/profile/webpush/subscribe" in page  # одноразовый запрос подписки в base.html
    assert "/cabinet/profile/webpush/subscribe" not in client.get("/").get_data(as_text=True)


def test_enable_push_switches_channel_when_email_not_ready(app, db, client):
    person = make_person(db, full_name="Безпочтов Без Безович")
    make_user(db, "member1", "pass12345", person=person)
    db.commit()

    _post_login(client, enable_push="1")
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel == NotificationChannel.WEBPUSH


def test_without_enable_push_nothing_changes(app, db, client):
    person = make_person(db, full_name="Безпочтов Без Безович")
    make_user(db, "member1", "pass12345", person=person)
    db.commit()

    _post_login(client)
    db.expire_all()
    assert db.query(User).filter_by(username="member1").one().notify_channel == NotificationChannel.EMAIL
    assert "/cabinet/profile/webpush/subscribe" not in client.get("/").get_data(as_text=True)


def test_login_form_has_checkboxes_and_consent_link(app, db, client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert 'name="enable_push"' in html
    assert 'name="pd_consent"' in html
    assert 'href="/personal-data-consent"' in html
    consent_tag = html[html.index('name="pd_consent"'):]
    assert "checked" not in consent_tag[:consent_tag.index(">")]

    client.set_cookie("pd_consent", "1")
    html = client.get("/auth/login").get_data(as_text=True)
    consent_tag = html[html.index('name="pd_consent"'):]
    assert "checked" in consent_tag[:consent_tag.index(">")]


def test_consent_page_is_public(app, db, client):
    html = client.get("/personal-data-consent").get_data(as_text=True)
    assert "Согласие на обработку персональных данных" in html
    assert "ч. 5 ст. 18" in html
    assert "1099–1101 Гражданского кодекса" in html
    assert "третьим лицам" not in html
