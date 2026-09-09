"""
Уведомления через Telegram (app/telegram_bot.py, app/notifications.py) —
второй реально работающий канал, наряду с email. В отличие от email,
"готовность" канала — не свободный текст Person.telegram, а привязанный
Person.telegram_chat_id (см. app/telegram_bot.py докстринг: Bot API не
позволяет написать первым, только ответить тому, кто сам прислал /start).

HTTP-запросы к Telegram подменяются на уровне requests.post/get (тот же
уровень, что реально вызывает app/telegram_bot.py) — не более глубокого
мока, чтобы заодно проверить, что тело/URL запроса собираются верно.
"""
import datetime as dt

from app import database
from app.models import RoleEnum, NotificationChannel, TelegramSettings, Person, User
from app.bank_api import crypto
from app import telegram_bot

from tests.conftest import make_person, make_user, login


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def _make_telegram_settings(db, bot_username="coop_bot", token="123:ABC"):
    settings = TelegramSettings(bot_username=bot_username, bot_token_encrypted=crypto.encrypt(token))
    db.add(settings)
    db.commit()
    return settings


# ---------------------------------------------------------------------------
# Готовность канала
# ---------------------------------------------------------------------------

def test_channel_not_ready_without_chat_id(app, db):
    from app.notifications import channel_is_ready
    person = make_person(db, full_name="Без Привязки")
    user = make_user(db, "notlinked", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    assert channel_is_ready(user, NotificationChannel.TELEGRAM) is False


def test_channel_ready_with_chat_id(app, db):
    from app.notifications import channel_is_ready
    person = make_person(db, full_name="С Привязкой", telegram_chat_id=555)
    user = make_user(db, "linked", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    assert channel_is_ready(user, NotificationChannel.TELEGRAM) is True


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def test_notify_sends_telegram_message_when_linked(app, db, monkeypatch):
    from app import notifications
    _make_telegram_settings(db)
    person = make_person(db, full_name="Получатель Уведомлений", telegram_chat_id=777)
    user = make_user(db, "recipient", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_channel = NotificationChannel.TELEGRAM
    user.notify_charge = True
    db.commit()

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append((url, json))
        return FakeResponse(200)

    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)

    notifications.notify(user, "charge", "Тема", "Текст сообщения")

    assert len(sent) == 1
    url, payload = sent[0]
    assert "123:ABC" in url
    assert payload["chat_id"] == 777
    assert "Тема" in payload["text"] and "Текст сообщения" in payload["text"]


def test_notify_noop_when_telegram_not_configured(app, db, monkeypatch):
    from app import notifications
    person = make_person(db, full_name="Без Настроек Бота", telegram_chat_id=888)
    user = make_user(db, "nobot", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_channel = NotificationChannel.TELEGRAM
    user.notify_charge = True
    db.commit()

    def fail_post(*a, **kw):
        raise AssertionError("не должно быть вызвано — бот не настроен")

    monkeypatch.setattr(telegram_bot.requests, "post", fail_post)
    notifications.notify(user, "charge", "Тема", "Текст")  # не должно упасть


# ---------------------------------------------------------------------------
# Настройки бота (/telegram/)
# ---------------------------------------------------------------------------

def test_only_chairman_can_view_settings(db, client):
    make_user(db, "board_tg", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_tg", "pass1234")
    resp = client.get("/telegram/")
    assert resp.status_code == 302


def test_chairman_can_save_settings_token_encrypted(db, client):
    make_user(db, "chair_tg", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_tg", "pass1234")

    resp = client.post("/telegram/settings", data={"bot_username": "@my_bot", "bot_token": "999:SECRET"})
    assert resp.status_code == 302

    settings = db.query(TelegramSettings).first()
    assert settings.bot_username == "my_bot"  # "@" срезан
    assert settings.bot_token_encrypted != "999:SECRET"
    assert crypto.decrypt(settings.bot_token_encrypted) == "999:SECRET"


def test_empty_token_on_resave_keeps_existing(db, client):
    make_user(db, "chair_tg2", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_tg2", "pass1234")

    client.post("/telegram/settings", data={"bot_username": "bot1", "bot_token": "111:AAA"})
    client.post("/telegram/settings", data={"bot_username": "bot1_renamed", "bot_token": ""})

    settings = db.query(TelegramSettings).first()
    assert settings.bot_username == "bot1_renamed"
    assert crypto.decrypt(settings.bot_token_encrypted) == "111:AAA"


# ---------------------------------------------------------------------------
# Привязка аккаунта из профиля
# ---------------------------------------------------------------------------

def test_telegram_link_start_generates_token(db, client):
    person = make_person(db, full_name="Хочет Привязать")
    make_user(db, "wantlink", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "wantlink", "pass1234")

    resp = client.post("/cabinet/profile/telegram/link")
    assert resp.status_code == 302
    db.refresh(person)
    assert person.telegram_link_token is not None


def test_profile_shows_deep_link_after_start(db, client):
    _make_telegram_settings(db, bot_username="coop_notify_bot")
    person = make_person(db, full_name="Ссылку Хочет")
    make_user(db, "wantlink2", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "wantlink2", "pass1234")

    client.post("/cabinet/profile/telegram/link")
    resp = client.get("/cabinet/profile")
    body = resp.get_data(as_text=True)
    db.refresh(person)
    assert f"https://t.me/coop_notify_bot?start={person.telegram_link_token}" in body


def test_telegram_unlink_clears_chat_id(db, client):
    person = make_person(db, full_name="Отвязать Хочет", telegram_chat_id=42)
    make_user(db, "wantunlink", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "wantunlink", "pass1234")

    resp = client.post("/cabinet/profile/telegram/unlink")
    assert resp.status_code == 302
    db.refresh(person)
    assert person.telegram_chat_id is None


# ---------------------------------------------------------------------------
# scripts/poll_telegram.py: app/telegram_bot.py:poll_and_link_accounts
# ---------------------------------------------------------------------------

def test_poll_and_link_accounts_links_matching_token(app, db, monkeypatch):
    settings = _make_telegram_settings(db)
    person = make_person(db, full_name="Ожидает Привязки")
    person.telegram_link_token = "sometoken123"
    db.commit()

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/getUpdates"):
            return FakeResponse(200, {"ok": True, "result": [
                {"update_id": 1, "message": {"text": "/start sometoken123", "chat": {"id": 999}}},
            ]})
        raise AssertionError(f"unexpected GET {url}")

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return FakeResponse(200)

    monkeypatch.setattr(telegram_bot.requests, "get", fake_get)
    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)

    total, linked = telegram_bot.poll_and_link_accounts()
    assert total == 1
    assert linked == 1

    db.refresh(person)
    assert person.telegram_chat_id == 999
    assert person.telegram_link_token is None
    db.refresh(settings)
    assert settings.last_update_id == 1
    assert len(sent) == 1  # приветственное сообщение


def test_poll_and_link_accounts_ignores_unknown_token(app, db, monkeypatch):
    settings = _make_telegram_settings(db)

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(200, {"ok": True, "result": [
            {"update_id": 5, "message": {"text": "/start nosuchtoken", "chat": {"id": 111}}},
        ]})

    def fake_post(url, json=None, timeout=None):
        raise AssertionError("не должно отправляться приветствие для неизвестного токена")

    monkeypatch.setattr(telegram_bot.requests, "get", fake_get)
    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)

    total, linked = telegram_bot.poll_and_link_accounts()
    assert total == 1
    assert linked == 0
    db.refresh(settings)
    assert settings.last_update_id == 5


def test_poll_and_link_accounts_noop_when_not_configured(app, db):
    total, linked = telegram_bot.poll_and_link_accounts()
    assert (total, linked) == (0, 0)
