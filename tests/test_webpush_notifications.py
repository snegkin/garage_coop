"""
Push-уведомления браузера (app/webpush.py, app/notifications.py) — третий
реально работающий канал, наряду с email и Telegram. В отличие от них,
подписок может быть несколько на одного пользователя (разные браузеры/
устройства) — notify() рассылает во все разом, а истекшие (404/410 от
push-сервиса) удаляются автоматически.

Сама отправка (pywebpush.webpush, шифрование payload) не мокается на
уровне HTTP — это чужая библиотека с собственными тестами; мокается сама
функция app.webpush.webpush, тот же уровень, на котором её вызывает наш
код (аналогично моку mail_client._connect_smtp/telegram_bot.requests.*
в других тестах уведомлений)."""
from app.models import RoleEnum, NotificationChannel, WebPushSettings, WebPushSubscription
from app import webpush
from app.bank_api import crypto
from pywebpush import WebPushException

from tests.conftest import make_person, make_user, login


def _make_webpush_settings(db):
    settings = WebPushSettings(
        public_key="pubkey123", private_key_encrypted=crypto.encrypt("dummy-private-key"),
        subject="mailto:test@example.com",
    )
    db.add(settings)
    db.commit()
    return settings


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "gone"


# ---------------------------------------------------------------------------
# Готовность канала / подписки
# ---------------------------------------------------------------------------

def test_channel_not_ready_without_subscription(app, db):
    from app.notifications import channel_is_ready
    person = make_person(db, full_name="Без Подписки")
    user = make_user(db, "nosub", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    assert channel_is_ready(user, NotificationChannel.WEBPUSH) is False


def test_channel_ready_with_subscription(app, db):
    from app.notifications import channel_is_ready
    person = make_person(db, full_name="С Подпиской")
    user = make_user(db, "hassub", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/1", "p256dh1", "auth1")
    assert channel_is_ready(user, NotificationChannel.WEBPUSH) is True


def test_subscribe_is_idempotent_per_endpoint(app, db):
    person = make_person(db, full_name="Дважды Подписан")
    user = make_user(db, "twicesub", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/2", "old", "old")
    webpush.subscribe(user.id, "https://push.example.com/2", "new", "new")
    rows = db.query(WebPushSubscription).filter_by(endpoint="https://push.example.com/2").all()
    assert len(rows) == 1
    assert rows[0].p256dh == "new"


def test_unsubscribe_removes_subscription(app, db):
    person = make_person(db, full_name="Отписывается")
    user = make_user(db, "unsub1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/3", "p", "a")
    webpush.unsubscribe(user.id, "https://push.example.com/3")
    assert db.query(WebPushSubscription).filter_by(endpoint="https://push.example.com/3").first() is None


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def test_notify_sends_to_all_subscriptions(app, db, monkeypatch):
    from app import notifications
    _make_webpush_settings(db)
    person = make_person(db, full_name="Много Устройств")
    user = make_user(db, "multidevice", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_channel = NotificationChannel.WEBPUSH
    user.notify_charge = True
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/a", "p1", "a1")
    webpush.subscribe(user.id, "https://push.example.com/b", "p2", "a2")

    sent = []

    def fake_webpush(subscription_info, data=None, vapid_private_key=None, vapid_claims=None):
        sent.append(subscription_info["endpoint"])

    monkeypatch.setattr(webpush, "webpush", fake_webpush)
    notifications.notify(user, "charge", "Тема", "Текст")

    assert sorted(sent) == ["https://push.example.com/a", "https://push.example.com/b"]


def test_notify_removes_expired_subscription(app, db, monkeypatch):
    from app import notifications
    _make_webpush_settings(db)
    person = make_person(db, full_name="Просроченная Подписка")
    user = make_user(db, "expiredsub", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_channel = NotificationChannel.WEBPUSH
    user.notify_charge = True
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/dead", "p", "a")

    def fake_webpush(subscription_info, data=None, vapid_private_key=None, vapid_claims=None):
        raise WebPushException("Push failed: 410 Gone", response=FakeResponse(410))

    monkeypatch.setattr(webpush, "webpush", fake_webpush)
    notifications.notify(user, "charge", "Тема", "Текст")

    assert db.query(WebPushSubscription).filter_by(endpoint="https://push.example.com/dead").first() is None


def test_notify_keeps_subscription_on_transient_error(app, db, monkeypatch):
    from app import notifications
    _make_webpush_settings(db)
    person = make_person(db, full_name="Временная Ошибка")
    user = make_user(db, "transienterr", "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_channel = NotificationChannel.WEBPUSH
    user.notify_charge = True
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/flaky", "p", "a")

    def fake_webpush(subscription_info, data=None, vapid_private_key=None, vapid_claims=None):
        raise WebPushException("Push failed: 500", response=FakeResponse(500))

    monkeypatch.setattr(webpush, "webpush", fake_webpush)
    notifications.notify(user, "charge", "Тема", "Текст")

    assert db.query(WebPushSubscription).filter_by(endpoint="https://push.example.com/flaky").first() is not None


# ---------------------------------------------------------------------------
# Роуты профиля
# ---------------------------------------------------------------------------

def test_webpush_subscribe_route_saves_subscription(db, client):
    person = make_person(db, full_name="Подписывается Через Форму")
    make_user(db, "routesub", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "routesub", "pass1234")

    resp = client.post("/cabinet/profile/webpush/subscribe", json={
        "endpoint": "https://push.example.com/route",
        "keys": {"p256dh": "pkey", "auth": "akey"},
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert db.query(WebPushSubscription).filter_by(endpoint="https://push.example.com/route").first() is not None


def test_webpush_subscribe_route_rejects_incomplete_payload(db, client):
    make_user(db, "badpayload", "pass1234", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "badpayload", "pass1234")

    resp = client.post("/cabinet/profile/webpush/subscribe", json={"endpoint": "https://push.example.com/bad"})
    assert resp.status_code == 400


def test_webpush_unsubscribe_route_removes_subscription(db, client):
    person = make_person(db, full_name="Отписывается Через Форму")
    user = make_user(db, "routeunsub", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/routeunsub", "p", "a")
    login(client, "routeunsub", "pass1234")

    resp = client.post("/cabinet/profile/webpush/unsubscribe", json={"endpoint": "https://push.example.com/routeunsub"})
    assert resp.status_code == 200
    assert db.query(WebPushSubscription).filter_by(endpoint="https://push.example.com/routeunsub").first() is None


def test_profile_page_exposes_vapid_public_key(db, client):
    person = make_person(db, full_name="Хочет Ключ")
    make_user(db, "wantkey", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "wantkey", "pass1234")

    resp = client.get("/cabinet/profile")
    body = resp.get_data(as_text=True)
    settings = db.query(WebPushSettings).first()
    assert settings is not None and settings.public_key
    assert f'data-vapid-key="{settings.public_key}"' in body
