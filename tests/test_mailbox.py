"""
Тесты роутов почты правления (app/mailbox.py) — через тестовый Flask-клиент,
с замоканными _connect_imap/_connect_smtp (см. tests/test_mail_client.py —
там же сама протокольная логика). Покрывает права доступа (BOARD читает и
пишет, CHAIRMAN настраивает), обработку ошибок подключения (не 500, флеш +
last_error), паттерн «пустой пароль — не менять», аудит (settings_save/
message_sent пишутся, неудачные попытки и test-connection — нет), и то,
что почта правления не создаёт побочных денежных сущностей.
"""
import io
from email.message import EmailMessage
import email.policy

from app import database, mail_client
from app.models import RoleEnum, MailboxSettings, MailProtocol, MailEncryption, AuditLog, Charge, Expense
from app.bank_api import crypto

from tests.conftest import make_person, make_user, login


def _make_board(db, username="board1"):
    person = make_person(db, full_name="Board One")
    make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()


def _make_chairman(db, username="chair1"):
    person = make_person(db, full_name="Chairman One")
    make_user(db, username, "pass1234", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()


def _make_member(db, username="member1"):
    person = make_person(db, full_name="Member One")
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()


def _make_settings(db, **overrides):
    defaults = dict(
        incoming_protocol=MailProtocol.IMAP, incoming_host="imap.example.com", incoming_port=993,
        incoming_encryption=MailEncryption.SSL, smtp_host="smtp.example.com", smtp_port=587,
        smtp_encryption=MailEncryption.STARTTLS, username="pravlenie@example.com",
        password_encrypted=crypto.encrypt("secret123"), from_name="Правление ГСК",
    )
    defaults.update(overrides)
    settings = MailboxSettings(**defaults)
    db.add(settings)
    db.commit()
    return settings


def _test_email(subject="Тестовое письмо", with_attachment=True):
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = "Отправитель <sender@example.com>"
    msg["To"] = "pravlenie@example.com"
    msg["Date"] = "Fri, 04 Sep 2026 12:00:00 +0300"
    msg.set_content("Текст письма")
    if with_attachment:
        msg.add_attachment(b"pdf-bytes", maintype="application", subtype="pdf", filename="act.pdf")
    return msg


class FakeImapConn:
    def __init__(self, messages):
        self._messages = messages

    def login(self, user, password):
        return ("OK", [b""])

    def select(self, folder):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "search":
            uids = " ".join(str(u) for u in sorted(self._messages)).encode()
            return ("OK", [uids])
        if command == "fetch":
            uid = int(args[0].decode() if isinstance(args[0], bytes) else args[0])
            spec = args[1] if len(args) > 1 else ""
            raw = self._messages.get(uid)
            if raw is None:
                return ("NO", [None])
            if "RFC822" in spec:
                return ("OK", [(f"{uid} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw)])
            meta = f"{uid} (UID {uid} FLAGS (\\Seen) BODY[HEADER.FIELDS (SUBJECT FROM DATE)] {{999}}".encode()
            return ("OK", [(meta, raw[:300])])
        return ("NO", [None])

    def close(self):
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


class FakeSmtpConn:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def _mock_imap(monkeypatch, messages):
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: FakeImapConn(messages))


def _mock_smtp(monkeypatch):
    fake = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake)
    return fake


# ---------------------------------------------------------------------------
# Права доступа
# ---------------------------------------------------------------------------

def test_member_is_redirected_from_all_routes(db, client):
    """roles_required редиректит с флеш-сообщением, а не отдаёт 403 — см.
    app/auth.py:roles_required."""
    _make_member(db)
    _make_settings(db)
    login(client, "member1", "pass1234")

    for resp in (
        client.get("/mailbox/", follow_redirects=True),
        client.get("/mailbox/compose", follow_redirects=True),
        client.post("/mailbox/settings", data={}, follow_redirects=True),
    ):
        assert resp.status_code == 200
        assert "Недостаточно прав" in resp.get_data(as_text=True)


def test_board_can_read_and_compose_but_not_change_settings(db, client, monkeypatch):
    _make_board(db)
    _make_settings(db)
    _mock_imap(monkeypatch, {1: _test_email().as_bytes()})
    login(client, "board1", "pass1234")

    assert client.get("/mailbox/").status_code == 200
    assert client.get("/mailbox/compose").status_code == 200

    resp = client.post("/mailbox/settings", data={"incoming_host": "hacked.example.com"}, follow_redirects=True)
    assert "Недостаточно прав" in resp.get_data(as_text=True)
    db.expire_all()
    settings = db.query(MailboxSettings).first()
    assert settings.incoming_host == "imap.example.com"  # не изменилось


def test_chairman_can_save_settings(db, client):
    _make_chairman(db)
    _make_settings(db)
    login(client, "chair1", "pass1234")

    resp = client.post("/mailbox/settings", data={
        "incoming_protocol": "imap", "incoming_host": "imap.new.com", "incoming_port": "993",
        "incoming_encryption": "ssl", "smtp_host": "smtp.new.com", "smtp_port": "587",
        "smtp_encryption": "starttls", "username": "pravlenie@example.com", "password": "",
        "from_name": "Правление",
    }, follow_redirects=True)
    assert "Настройки почты сохранены" in resp.get_data(as_text=True)

    db.expire_all()
    settings = db.query(MailboxSettings).first()
    assert settings.incoming_host == "imap.new.com"


# ---------------------------------------------------------------------------
# Пустой пароль — не менять
# ---------------------------------------------------------------------------

def test_empty_password_does_not_clear_existing_secret(db, client):
    _make_chairman(db)
    settings = _make_settings(db)
    original_encrypted = settings.password_encrypted
    login(client, "chair1", "pass1234")

    client.post("/mailbox/settings", data={
        "incoming_protocol": "imap", "incoming_host": "imap.example.com", "incoming_port": "993",
        "incoming_encryption": "ssl", "smtp_host": "smtp.example.com", "smtp_port": "587",
        "smtp_encryption": "starttls", "username": "pravlenie@example.com", "password": "",
    }, follow_redirects=True)

    db.expire_all()
    settings = db.query(MailboxSettings).first()
    assert settings.password_encrypted == original_encrypted
    assert crypto.decrypt(settings.password_encrypted) == "secret123"


def test_nonempty_password_replaces_secret(db, client):
    _make_chairman(db)
    _make_settings(db)
    login(client, "chair1", "pass1234")

    client.post("/mailbox/settings", data={
        "incoming_protocol": "imap", "incoming_host": "imap.example.com", "incoming_port": "993",
        "incoming_encryption": "ssl", "smtp_host": "smtp.example.com", "smtp_port": "587",
        "smtp_encryption": "starttls", "username": "pravlenie@example.com", "password": "newpass456",
    }, follow_redirects=True)

    db.expire_all()
    settings = db.query(MailboxSettings).first()
    assert crypto.decrypt(settings.password_encrypted) == "newpass456"


# ---------------------------------------------------------------------------
# Просмотр/вложения
# ---------------------------------------------------------------------------

def test_inbox_lists_messages(db, client, monkeypatch):
    _make_board(db)
    _make_settings(db)
    _mock_imap(monkeypatch, {1: _test_email(subject="Уникальная тема 42").as_bytes()})
    login(client, "board1", "pass1234")

    resp = client.get("/mailbox/")
    assert resp.status_code == 200
    assert "Уникальная тема 42" in resp.get_data(as_text=True)


def test_inbox_without_settings_shows_not_configured(db, client):
    _make_board(db)
    login(client, "board1", "pass1234")
    resp = client.get("/mailbox/")
    assert resp.status_code == 200
    assert "не настроена" in resp.get_data(as_text=True)


def test_connection_error_shows_flash_not_500(db, client, monkeypatch):
    _make_board(db)
    _make_settings(db)

    def boom(settings):
        raise mail_client.MailError("IMAP: Connection refused")

    monkeypatch.setattr(mail_client, "_connect_imap", boom)
    login(client, "board1", "pass1234")

    resp = client.get("/mailbox/")
    assert resp.status_code == 200
    assert "Не удалось подключиться" in resp.get_data(as_text=True)

    db.expire_all()
    settings = db.query(MailboxSettings).first()
    assert "Connection refused" in settings.last_error


def test_non_ascii_username_shows_flash_not_generic_form_error(db, client, monkeypatch):
    """Регресс: до фикса UnicodeEncodeError (подкласс ValueError) из
    login() с кириллическим логином проскакивал мимо MailError и попадал в
    общий обработчик форм (app/errors.py) — страница почты вообще
    переставала открываться, пользователь видел не относящееся к делу
    "проверьте правильность заполнения формы"."""
    _make_board(db)
    _make_settings(db, username="логин@пример.рф")

    class FakeConnBadLogin:
        def login(self, user, password):
            raise UnicodeEncodeError("ascii", user, 0, 1, "ordinal not in range(128)")

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: FakeConnBadLogin())
    login(client, "board1", "pass1234")

    resp = client.get("/mailbox/", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Не удалось подключиться" in body
    assert "Проверьте правильность заполнения формы" not in body


def test_view_message_and_download_attachment(db, client, monkeypatch):
    _make_board(db)
    _make_settings(db)
    _mock_imap(monkeypatch, {1: _test_email().as_bytes()})
    login(client, "board1", "pass1234")

    resp = client.get("/mailbox/messages/1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "act.pdf" in body

    resp2 = client.get("/mailbox/messages/1/attachments/1")
    assert resp2.status_code == 200
    assert resp2.data == b"pdf-bytes"


def test_external_images_blocked_by_default_and_shown_on_request(db, client, monkeypatch):
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = "С картинкой"
    msg["From"] = "x@example.com"
    msg["To"] = "pravlenie@example.com"
    msg["Date"] = "Fri, 04 Sep 2026 12:00:00 +0300"
    msg.add_alternative("<img src='https://tracker.example/pixel.gif'>", subtype="html")

    _make_board(db)
    _make_settings(db)
    _mock_imap(monkeypatch, {1: msg.as_bytes()})
    login(client, "board1", "pass1234")

    resp = client.get("/mailbox/messages/1")
    assert "tracker.example" not in resp.get_data(as_text=True)
    assert "Показать изображения" in resp.get_data(as_text=True)

    resp2 = client.get("/mailbox/messages/1?allow_images=1")
    assert "tracker.example" in resp2.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def test_compose_sends_with_attachment(db, client, monkeypatch):
    _make_board(db)
    _make_settings(db)
    fake_smtp = _mock_smtp(monkeypatch)
    login(client, "board1", "pass1234")

    resp = client.post("/mailbox/compose", data={
        "to": "someone@example.com, other@example.com",
        "subject": "Повестка",
        "body": "Текст письма",
        "attachments": (io.BytesIO(b"file content"), "report.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert resp.status_code == 200
    assert "Письмо отправлено" in resp.get_data(as_text=True)
    assert len(fake_smtp.sent) == 1
    sent = fake_smtp.sent[0]
    assert sent["Subject"] == "Повестка"
    assert "someone@example.com" in sent["To"] and "other@example.com" in sent["To"]
    assert sum(1 for _ in sent.iter_attachments()) == 1


def test_compose_send_failure_does_not_lose_form_data(db, client, monkeypatch):
    _make_board(db)
    _make_settings(db)

    def boom(settings):
        raise mail_client.MailError("SMTP: auth failed")

    monkeypatch.setattr(mail_client, "_connect_smtp", boom)
    login(client, "board1", "pass1234")

    resp = client.post("/mailbox/compose", data={
        "to": "someone@example.com", "subject": "Тема не должна потеряться", "body": "Текст не должен потеряться",
    }, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Не удалось отправить письмо" in body
    assert "Тема не должна потеряться" in body
    assert "Текст не должен потеряться" in body


def test_sending_mail_creates_no_charge_or_expense(db, client, monkeypatch):
    """Регрессия: почта правления не денежная операция — не должна
    затрагивать Charge/Expense ни при каких обстоятельствах."""
    _make_board(db)
    _make_settings(db)
    _mock_smtp(monkeypatch)
    login(client, "board1", "pass1234")

    charges_before = db.query(Charge).count()
    expenses_before = db.query(Expense).count()
    client.post("/mailbox/compose", data={"to": "x@example.com", "subject": "s", "body": "b"}, follow_redirects=True)
    assert db.query(Charge).count() == charges_before
    assert db.query(Expense).count() == expenses_before


# ---------------------------------------------------------------------------
# Аудит
# ---------------------------------------------------------------------------

def test_settings_save_and_message_sent_write_audit_log(db, client, monkeypatch):
    _make_chairman(db)
    _make_board(db)
    _make_settings(db)
    _mock_smtp(monkeypatch)

    login(client, "chair1", "pass1234")
    client.post("/mailbox/settings", data={
        "incoming_protocol": "imap", "incoming_host": "imap.example.com", "incoming_port": "993",
        "incoming_encryption": "ssl", "smtp_host": "smtp.example.com", "smtp_port": "587",
        "smtp_encryption": "starttls", "username": "pravlenie@example.com", "password": "",
    }, follow_redirects=True)
    client.get("/auth/logout")

    login(client, "board1", "pass1234")
    client.post("/mailbox/compose", data={"to": "x@example.com", "subject": "s", "body": "b"}, follow_redirects=True)

    actions = [a.action for a in db.query(AuditLog).all()]
    assert "mailbox.settings_save" in actions
    assert "mailbox.message_sent" in actions


def test_failed_connection_test_is_not_audited(db, client, monkeypatch):
    _make_chairman(db)
    _make_settings(db)

    def boom(settings):
        raise mail_client.MailError("boom")

    monkeypatch.setattr(mail_client, "test_incoming_connection", boom)
    monkeypatch.setattr(mail_client, "test_smtp_connection", boom)
    login(client, "chair1", "pass1234")

    client.post("/mailbox/settings/test-connection", follow_redirects=True)
    actions = [a.action for a in db.query(AuditLog).all() if a.action.startswith("mailbox.")]
    assert actions == []
