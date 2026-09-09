"""
Уведомления о событиях сайта по подписке (app/notifications.py) —
начисление/платёж на свой счёт, новость/объявление, форум, чат правления.
Реально отправляется только email — тесты подменяют mail_client._connect_smtp
(тот же приём, что и в tests/test_mailbox.py: FakeSmtpConn), чтобы не лезть
в реальную сеть.

Настройки в профиле (app/cabinet.py: profile) сохраняются сразу (не через
PersonDataRevision) и валидируются против ТЕКУЩИХ (уже одобренных) полей
person — контакт, вписанный в этой же форме, ещё не применён.
"""
import datetime as dt
from decimal import Decimal

from app import database, mail_client
from app.models import RoleEnum, FeeType, MemberAccount, Charge, Payment, News, BulletinCategory, User

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


class FakeSmtpConn:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def _mock_smtp(monkeypatch):
    fake = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake)
    return fake


def _make_mailbox_settings(db):
    from app.models import MailboxSettings, MailProtocol, MailEncryption
    from app.bank_api import crypto
    settings = MailboxSettings(
        incoming_protocol=MailProtocol.IMAP, incoming_host="imap.example.com", incoming_port=993,
        incoming_encryption=MailEncryption.SSL, smtp_host="smtp.example.com", smtp_port=587,
        smtp_encryption=MailEncryption.STARTTLS, username="pravlenie@example.com",
        password_encrypted=crypto.encrypt("secret123"), from_name="Правление ГСК",
    )
    db.add(settings)
    db.commit()
    return settings


def _setup_account(db, account_number="19001", email="member@example.com"):
    person = make_person(db, full_name="Уведомляемый Член Кооператива", email=email)
    garage = make_garage(db, number="90")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="dues", name="Взнос")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=account_number)
    db.add(account)
    user = make_user(db, "member_notify", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.flush()
    return account, person, user


# ---------------------------------------------------------------------------
# Настройки в профиле
# ---------------------------------------------------------------------------

def test_cannot_switch_to_telegram_without_linked_account(app, db, client):
    """По умолчанию канал — email (см. миграцию e379b7dc72bb), но
    переключиться на Telegram без привязанного telegram_chat_id нельзя —
    форма отклоняется, канал остаётся прежним (email)."""
    person = make_person(db, full_name="Без Telegram Человек")
    make_user(db, "notg", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "notg", "pass1234")

    resp = client.post("/cabinet/profile/notifications", data={
        "notify_channel": "telegram", "notify_charge": "1",
    })
    assert resp.status_code == 302
    db.expire_all()
    user = db.query(User).filter_by(username="notg").first()
    assert user.notify_channel.value == "email"


def test_can_enable_email_channel_with_email_set(app, db, client):
    person = make_person(db, full_name="С Почтой Человек", email="me@example.com")
    make_user(db, "withemail", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "withemail", "pass1234")

    resp = client.post("/cabinet/profile/notifications", data={
        "notify_channel": "email", "notify_charge": "1", "notify_payment": "1",
    })
    assert resp.status_code == 302
    db.expire_all()
    user = db.query(User).filter_by(username="withemail").first()
    assert user.notify_channel.value == "email"
    assert user.notify_charge is True
    assert user.notify_payment is True


def test_board_chat_checkbox_ignored_for_non_board(app, db, client):
    person = make_person(db, full_name="Рядовой Член", email="rank@example.com")
    make_user(db, "rankmember", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "rankmember", "pass1234")

    client.post("/cabinet/profile/notifications", data={
        "notify_channel": "email", "notify_board_chat": "1",
    })
    db.expire_all()
    user = db.query(User).filter_by(username="rankmember").first()
    assert user.notify_board_chat is False


# ---------------------------------------------------------------------------
# Начисление / платёж
# ---------------------------------------------------------------------------

def test_charge_notification_sent_when_subscribed(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    account, person, user = _setup_account(db)
    from app.models import NotificationChannel
    user.notify_channel = NotificationChannel.EMAIL
    user.notify_charge = True
    make_user(db, "board_actor", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_actor", "pass1234")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/charges/add",
        data={"year": "2026", "amount": "500.00", "comment": ""},
    )
    assert resp.status_code == 302
    assert len(fake_smtp.sent) == 1
    assert person.email in fake_smtp.sent[0]["To"]


def test_charge_notification_not_sent_when_not_subscribed(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    account, person, user = _setup_account(db)
    user.notify_charge = False  # явный отказ от подписки (по умолчанию включена)
    make_user(db, "board_actor2", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_actor2", "pass1234")

    client.post(
        f"/finance/member-accounts/{account.id}/charges/add",
        data={"year": "2026", "amount": "500.00", "comment": ""},
    )
    assert len(fake_smtp.sent) == 0


def test_payment_notification_sent_when_subscribed(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    account, person, user = _setup_account(db)
    from app.models import NotificationChannel
    user.notify_channel = NotificationChannel.EMAIL
    user.notify_payment = True
    make_user(db, "board_actor3", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_actor3", "pass1234")

    resp = client.post(
        f"/finance/member-accounts/{account.id}/payments/add",
        data={"date": "2026-01-15", "amount": "300.00", "comment": ""},
    )
    assert resp.status_code == 302
    assert len(fake_smtp.sent) == 1


# ---------------------------------------------------------------------------
# Новости / доска объявлений (общий тип подписки "news")
# ---------------------------------------------------------------------------

def _make_subscriber(db, username, event_field, email="sub@example.com"):
    from app.models import NotificationChannel
    person = make_person(db, full_name=f"Подписчик {username}", email=email)
    user = make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    user.notify_channel = NotificationChannel.EMAIL
    setattr(user, event_field, True)
    db.flush()
    return user


def test_news_publish_notifies_subscriber_not_author(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    subscriber = _make_subscriber(db, "news_sub", "notify_news")
    board_person = make_person(db, full_name="Правление Новостник", email="board@example.com")
    board = make_user(db, "news_board", "pass1234", role=RoleEnum.BOARD, person=board_person)
    board.notify_news = True  # автор тоже подписан — не должен получить письмо о своей же новости
    from app.models import NotificationChannel
    board.notify_channel = NotificationChannel.EMAIL
    db.commit()
    login(client, "news_board", "pass1234")

    resp = client.post("/news/new", data={"title": "Заголовок новости", "body": "Текст"})
    assert resp.status_code == 302
    assert len(fake_smtp.sent) == 1
    assert "sub@example.com" in fake_smtp.sent[0]["To"]


def test_bulletin_publish_notifies_news_subscriber(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _make_subscriber(db, "bulletin_sub", "notify_news")
    author_person = make_person(db, full_name="Объявитель", email="author@example.com")
    make_user(db, "bulletin_author", "pass1234", role=RoleEnum.MEMBER, person=author_person)
    db.commit()
    login(client, "bulletin_author", "pass1234")

    resp = client.post("/bulletin/new", data={
        "category": BulletinCategory.SELL.value,
        "title": "Продам", "description": "Описание", "contact": "8-900-000-00-00",
    })
    assert resp.status_code == 302
    assert len(fake_smtp.sent) == 1


# ---------------------------------------------------------------------------
# Форум
# ---------------------------------------------------------------------------

def test_forum_new_topic_notifies_subscriber_not_author(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _make_subscriber(db, "forum_sub", "notify_forum")
    author_person = make_person(db, full_name="Форумчанин", email="topicauthor@example.com")
    make_user(db, "forum_author", "pass1234", role=RoleEnum.MEMBER, person=author_person)
    db.commit()
    login(client, "forum_author", "pass1234")

    resp = client.post("/forum/new", data={"title": "Тема", "body": "Текст темы"})
    assert resp.status_code == 302
    assert len(fake_smtp.sent) == 1


def test_forum_reply_notifies_only_participants(app, db, client, monkeypatch):
    fake_smtp = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    # Автор темы подписан на форум — должен получить письмо об ответе.
    topic_author = _make_subscriber(db, "topic_author", "notify_forum", email="ta@example.com")
    # Посторонний подписчик форума, НЕ участвовавший в теме — письма быть не должно.
    _make_subscriber(db, "outsider", "notify_forum", email="outsider@example.com")
    replier_person = make_person(db, full_name="Отвечающий", email="replier@example.com")
    make_user(db, "replier", "pass1234", role=RoleEnum.MEMBER, person=replier_person)
    db.commit()

    login(client, "topic_author", "pass1234")
    resp = client.post("/forum/new", data={"title": "Тема для ответа", "body": "Начало"})
    assert resp.status_code == 302
    topic_url = resp.headers["Location"]
    topic_id = int(topic_url.rstrip("/").rsplit("/", 1)[-1])
    fake_smtp.sent.clear()  # игнорируем письмо про саму тему (её отправили другому подписчику)
    client.get("/auth/logout")

    login(client, "replier", "pass1234")
    resp = client.post(f"/forum/{topic_id}/reply", data={"body": "Ответ"})
    assert resp.status_code == 302
    assert len(fake_smtp.sent) == 1
    assert "ta@example.com" in fake_smtp.sent[0]["To"]
