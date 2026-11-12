"""
Уведомления людям через MAX (app/max_bot.py, app/notifications.py):
привязка аккаунта по диплинку (bot_started с токеном), галочка в профиле,
доставка через очередь в личный диалог (POST /messages?user_id=…).
"""
from app import max_bot, messenger_outbox, notifications
from app.bank_api import crypto
from app.models import MaxSettings, MessengerOutbox, NotificationChannel, RoleEnum

from tests.conftest import make_person, make_user, login
from tests.test_max_news import FakeMax, FakeResponse


def _settings(db, bot_username="id7700000000_bot"):
    settings = MaxSettings(bot_token_encrypted=crypto.encrypt("max-token"), bot_username=bot_username)
    db.add(settings)
    db.commit()
    return settings


def _updates(monkeypatch, fake, batch):
    original = fake.request

    def request(method, url, params=None, **kw):
        if url.endswith("/updates"):
            return FakeResponse(200, batch)
        return original(method, url, params=params, **kw)

    monkeypatch.setattr(max_bot.requests, "request", request)


def test_channel_ready_only_with_user_id(app, db):
    person = make_person(db, full_name="Без Привязки MAX")
    user = make_user(db, "maxnolink", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    assert notifications.channel_is_ready(user, NotificationChannel.MAX) is False
    person.max_user_id = 123
    db.commit()
    assert notifications.channel_is_ready(user, NotificationChannel.MAX) is True


def test_notify_goes_to_max_dialog_via_queue(app, db, monkeypatch):
    _settings(db)
    fake = FakeMax(monkeypatch)
    person = make_person(db, full_name="Получатель MAX", max_user_id=4242)
    user = make_user(db, "maxrecipient", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_via_email = False
    user.notify_via_max = True
    user.notify_charge = True
    db.commit()

    notifications.notify(user, "charge", "Тема", "Текст сообщения")
    assert fake.messages == []
    out = db.query(MessengerOutbox).one()
    assert out.channel == "max" and out.kind == "message"
    assert messenger_outbox.process(app.config["UPLOAD_FOLDER"]) == (1, 0)
    [msg] = fake.messages
    assert msg["params"] == {"user_id": "4242"}
    assert msg["body"]["text"] == "Тема\n\nТекст сообщения" and "format" not in msg["body"]


def test_notify_skipped_when_bot_not_configured(app, db):
    person = make_person(db, full_name="Нет Бота MAX", max_user_id=4242)
    user = make_user(db, "maxnobot", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_via_email = False
    user.notify_via_max = True
    user.notify_charge = True
    db.commit()
    notifications.notify(user, "charge", "Тема", "Текст")
    assert db.query(MessengerOutbox).count() == 0


def test_bot_started_links_account(app, db, monkeypatch):
    _settings(db)
    fake = FakeMax(monkeypatch)
    person = make_person(db, full_name="Ждёт Привязки MAX")
    person.max_link_token = "abc123"
    other = make_person(db, full_name="Чужой")
    other.max_link_token = "zzz999"
    db.commit()
    _updates(monkeypatch, fake, {"updates": [
        {"update_type": "bot_started", "chat_id": 9001, "user": {"user_id": 555, "name": "Иван"}, "payload": "abc123"},
        {"update_type": "bot_started", "chat_id": 9002, "user": {"user_id": 556}, "payload": "wrong"},
        {"update_type": "bot_started", "chat_id": 9003, "user": {"user_id": 557}, "payload": None},
    ], "marker": 7})

    assert max_bot.poll_updates() == (3, 1)
    db.refresh(person)
    db.refresh(other)
    assert person.max_user_id == 555 and person.max_link_token is None
    assert other.max_user_id is None and other.max_link_token == "zzz999"
    [welcome] = fake.messages
    assert welcome["params"] == {"user_id": 555} and "Готово" in welcome["body"]["text"]
    assert max_bot.known_chats(db.query(MaxSettings).one()) == []  # личный диалог — не канал


def test_profile_link_flow(db, client):
    _settings(db)
    person = make_person(db, full_name="Хочет MAX")
    make_user(db, "maxwantlink", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "maxwantlink", "pass1234")
    assert "Привязать MAX" in client.get("/cabinet/profile").get_data(as_text=True)

    client.post("/cabinet/profile/max/link")
    db.refresh(person)
    assert person.max_link_token and all(c in "0123456789abcdef" for c in person.max_link_token)
    body = client.get("/cabinet/profile").get_data(as_text=True)
    assert f"https://max.ru/id7700000000_bot?start={person.max_link_token}" in body


def test_profile_hides_max_when_bot_not_configured(db, client):
    person = make_person(db, full_name="Без Бота")
    make_user(db, "maxhidden", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "maxhidden", "pass1234")
    assert 'name="notify_via_max"' not in client.get("/cabinet/profile").get_data(as_text=True)


def test_cannot_enable_max_without_link(db, client):
    _settings(db)
    person = make_person(db, full_name="Не Привязан", email="a@example.org")
    user = make_user(db, "maxnotready", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "maxnotready", "pass1234")
    client.post("/cabinet/profile/notifications", data={"notify_via_max": "1"})
    db.refresh(user)
    assert user.notify_via_max is False

    person.max_user_id = 77
    db.commit()
    client.post("/cabinet/profile/notifications", data={"notify_via_max": "1"})
    db.refresh(user)
    assert user.notify_via_max is True


def test_unlink_clears_user_id(db, client):
    person = make_person(db, full_name="Отвязать MAX", max_user_id=77)
    user = make_user(db, "maxunlink", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_via_max = True
    db.commit()
    login(client, "maxunlink", "pass1234")
    client.post("/cabinet/profile/max/unlink")
    db.refresh(person)
    db.refresh(user)
    assert person.max_user_id is None and user.notify_via_max is False


def test_test_notification_reports_max_error(app, db, monkeypatch):
    _settings(db)
    FakeMax(monkeypatch, script=[FakeResponse(403, {"code": "chat.denied", "message": "bot blocked"})])
    person = make_person(db, full_name="Заблокировал Бота", max_user_id=77)
    user = make_user(db, "maxtest", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    ok, message = notifications._test_channel(user, NotificationChannel.MAX, "Тест", "Текст")
    assert ok is False and "chat.denied" in message
