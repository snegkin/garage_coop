"""
Способы доставки уведомлений — независимые галочки в профиле
(User.notify_via_email/telegram/webpush, cabinet.notification_settings):
можно несколько сразу, ни одной — уведомления не приходят. Подписку
этого браузера на push оформляет JS профиля при «Сохранить» (здесь — без
браузера, подписка заводится напрямую, как это сделал бы JS).
"""
from app import notifications, webpush
from app.models import RoleEnum, User

from tests.conftest import make_person, make_user, login
from tests.test_notifications import _mock_smtp, _make_mailbox_settings
from tests.test_webpush_notifications import _make_webpush_settings


def _user(db, email="multi@example.com", telegram_chat_id=None):
    person = make_person(db, full_name="Многоканальный Человек", email=email, telegram_chat_id=telegram_chat_id)
    user = make_user(db, "multi", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    return user


def _save(client, **fields):
    return client.post("/cabinet/profile/notifications", data={"notify_charge": "1", **fields})


def _reload(db):
    db.expire_all()
    return db.query(User).filter_by(username="multi").one()


def test_several_channels_saved_at_once(app, db, client):
    _user(db, telegram_chat_id="12345")
    login(client, "multi", "pass1234")

    _save(client, notify_via_email="1", notify_via_telegram="1")
    user = _reload(db)
    assert (user.notify_via_email, user.notify_via_telegram, user.notify_via_webpush) == (True, True, False)


def test_no_channel_ticked_means_no_notifications(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _user(db)
    login(client, "multi", "pass1234")

    _save(client)
    user = _reload(db)
    assert (user.notify_via_email, user.notify_via_telegram, user.notify_via_webpush) == (False, False, False)
    notifications.notify(user, "charge", "Тема", "Текст")
    assert fake.sent == []


def test_push_without_subscription_is_rejected(app, db, client):
    _user(db)
    login(client, "multi", "pass1234")

    resp = _save(client, notify_via_email="1", notify_via_webpush="1")
    assert resp.status_code == 302
    user = _reload(db)
    assert user.notify_via_webpush is None  # не сохранено


def test_push_with_subscription_is_saved(app, db, client):
    user = _user(db)
    webpush.subscribe(user.id, "https://push.example.com/x", "p", "a")
    login(client, "multi", "pass1234")

    _save(client, notify_via_webpush="1")
    user = _reload(db)
    assert (user.notify_via_email, user.notify_via_webpush) == (False, True)


def test_notify_delivers_to_every_ticked_channel(app, db, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _make_webpush_settings(db)
    user = _user(db)
    user.notify_via_webpush = True
    db.commit()
    webpush.subscribe(user.id, "https://push.example.com/x", "p", "a")
    pushed = []
    monkeypatch.setattr(webpush, "webpush", lambda subscription_info, **kw: pushed.append(subscription_info["endpoint"]))

    notifications.notify(user, "charge", "Тема", "Текст")
    assert len(fake.sent) == 1
    assert pushed == ["https://push.example.com/x"]


def test_ticked_but_not_ready_channel_is_skipped(app, db, monkeypatch):
    """Telegram отмечен, но привязку сбросили — email всё равно доходит."""
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    user = _user(db)
    user.notify_via_telegram = True
    db.commit()

    notifications.notify(user, "charge", "Тема", "Текст")
    assert len(fake.sent) == 1


def test_test_button_reports_each_channel(app, db, client, monkeypatch):
    _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    user = _user(db)
    user.notify_via_telegram = True  # не привязан
    db.commit()
    login(client, "multi", "pass1234")

    html = client.post("/cabinet/profile/notifications/test", follow_redirects=True).get_data(as_text=True)
    assert "Email: Тестовое уведомление отправлено" in html
    assert "Telegram: Чтобы получать уведомления этим способом" in html


def test_profile_shows_channel_checkboxes_without_push_buttons(app, db, client):
    _user(db)
    login(client, "multi", "pass1234")

    html = client.get("/cabinet/profile").get_data(as_text=True)
    assert 'name="notify_via_email"' in html
    assert 'name="notify_via_telegram"' in html
    assert 'name="notify_via_webpush"' in html
    assert 'name="notify_channel"' not in html
    assert "webpushSubscribeBtn" not in html and "webpushUnsubscribeBtn" not in html


def test_persons_list_shows_notification_channels(app, db, client):
    """Колонка «Уведомления» реестра людей: иконка на каждый отмеченный
    способ, бледная — если он не работает; без учётной записи — «—»."""
    user = _user(db)  # email отмечен и указан
    user.notify_via_telegram = True  # отмечен, но не привязан
    no_channels = make_user(db, "silent", "pass1234", person=make_person(db, full_name="Тихий Человек"))
    no_channels.notify_via_email = False
    make_person(db, full_name="Без Учётки Человек")
    make_user(db, "boss", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "boss", "pass1234")

    html = client.get("/persons/").get_data(as_text=True)
    assert "<th>Уведомления</th>" in html
    row = html[html.index("Многоканальный Человек"):]
    row = row[:row.index("</tr>")]
    assert 'class="notify-icon" title="Email"' in row
    assert 'class="notify-icon notify-icon-off" title="Telegram отмечен, но не привязан"' in row

    silent = html[html.index("Тихий Человек"):]
    assert "не получает" in silent[:silent.index("</tr>")]
    nobody = html[html.index("Без Учётки Человек"):]
    assert "Нет учётной записи для входа" in nobody[:nobody.index("</tr>")]
