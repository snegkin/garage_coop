"""
Тесты протокольной логики почты правления (app/mail_client.py) — без Flask
и без реального почтового сервера: мокаются только три точки подключения
(_connect_imap/_connect_pop3/_connect_smtp), вся остальная логика (разбор
MIME, пагинация, флаги, сборка исходящего письма) исполняется по-настоящему
и проверяется как есть — см. докстринг mail_client.py.

Особое внимание — разбору письма с inline-картинкой, вложенной в
multipart/related внутри multipart/alternative (типичная структура HTML-
писем): iter_attachments() эту структуру не разворачивает (реальный баг,
пойманный при ручной проверке), поэтому _parse_message использует
msg.walk() — тест test_parse_message_finds_inline_image_in_nested_related
это фиксирует как регресс.
"""
import datetime as dt
from decimal import Decimal
from email.message import EmailMessage
import email.policy

import pytest

from app import mail_client
from app.mail_client import MailError
from app.models import MailboxSettings, MailProtocol, MailEncryption


def _make_test_email(subject="Тема письма", with_inline_image=True, with_attachment=True):
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = "Правление ГСК <pravlenie@example.com>"
    msg["To"] = "member@example.com"
    msg["Date"] = "Fri, 04 Sep 2026 12:00:00 +0300"
    msg.set_content("Обычный текст письма")
    msg.add_alternative("<p>HTML <b>тело</b></p><img src='cid:logo1'>", subtype="html")
    if with_inline_image:
        html_part = msg.get_body(preferencelist=("html",))
        html_part.add_related(b"\x89PNGDATA", maintype="image", subtype="png", cid="<logo1>")
    if with_attachment:
        msg.add_attachment(b"pdf-bytes", maintype="application", subtype="pdf", filename="act.pdf")
    return msg


def test_parse_message_decodes_subject_and_addresses():
    msg = _make_test_email(subject="Привет, кириллица")
    detail = mail_client._parse_message("1", msg.as_bytes())
    assert detail.subject == "Привет, кириллица"
    assert detail.from_name == "Правление ГСК"
    assert detail.from_addr == "pravlenie@example.com"
    assert detail.to_addrs == ["member@example.com"]
    assert detail.date == dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))


def test_parse_message_extracts_both_bodies():
    msg = _make_test_email()
    detail = mail_client._parse_message("1", msg.as_bytes())
    assert detail.body_text and "Обычный текст" in detail.body_text
    assert detail.body_html and "HTML" in detail.body_html


def test_parse_message_finds_inline_image_in_nested_related():
    """Регресс: iter_attachments() не рекурсирует в multipart/related,
    вложенный в multipart/alternative — inline-картинка терялась бы."""
    msg = _make_test_email(with_inline_image=True)
    detail = mail_client._parse_message("1", msg.as_bytes())
    assert "logo1" in detail.inline_images
    part, data = detail.inline_images["logo1"]
    assert data == b"\x89PNGDATA"
    assert part.content_type == "image/png"


def test_parse_message_lists_attachment_without_duplicating_inline_image():
    msg = _make_test_email(with_inline_image=True, with_attachment=True)
    detail = mail_client._parse_message("1", msg.as_bytes())
    assert len(detail.attachments) == 1
    assert detail.attachments[0].filename == "act.pdf"


def test_extract_attachment_by_index_matches_listed_index():
    msg = _make_test_email(with_inline_image=True, with_attachment=True)
    raw = msg.as_bytes()
    detail = mail_client._parse_message("1", raw)
    att_index = detail.attachments[0].index
    result = mail_client._extract_attachment(raw, att_index)
    assert result is not None
    part, data = result
    assert data == b"pdf-bytes"
    assert part.filename == "act.pdf"


def test_parse_message_without_attachments_or_inline():
    msg = _make_test_email(with_inline_image=False, with_attachment=False)
    detail = mail_client._parse_message("1", msg.as_bytes())
    assert detail.attachments == []
    assert detail.inline_images == {}


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

class FakeImapConn:
    """Минимальный двойник imaplib.IMAP4 — поддерживает ровно те вызовы,
    которые делает ImapMailClient."""

    def __init__(self, messages: dict[int, bytes]):
        self._messages = messages  # uid -> raw bytes

    def login(self, user, password):
        return ("OK", [b"logged in"])

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
        return ("BYE", [b"bye"])


def _imap_settings():
    return MailboxSettings(
        incoming_protocol=MailProtocol.IMAP, incoming_host="imap.example.com", incoming_port=993,
        incoming_encryption=MailEncryption.SSL, smtp_host="smtp.example.com", smtp_port=587,
        smtp_encryption=MailEncryption.STARTTLS, username="pravlenie@example.com", password_encrypted="",
    )


def test_imap_list_messages_newest_first_and_pagination(monkeypatch):
    msgs = {i: _make_test_email(subject=f"Письмо {i}", with_inline_image=False, with_attachment=False).as_bytes() for i in range(1, 6)}
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: FakeImapConn(msgs))

    client = mail_client.get_incoming_client(_imap_settings())
    page = client.list_messages(page=1, page_size=2)
    assert page.total == 5
    assert [m.uid for m in page.messages] == ["5", "4"]
    assert page.has_next is True
    assert page.has_prev is False

    page2 = client.list_messages(page=3, page_size=2)
    assert [m.uid for m in page2.messages] == ["1"]
    assert page2.has_next is False
    client.close()


def test_imap_get_message_roundtrip(monkeypatch):
    msgs = {7: _make_test_email(subject="Прочитать меня").as_bytes()}
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: FakeImapConn(msgs))

    with mail_client.get_incoming_client(_imap_settings()) as client:
        detail = client.get_message("7")
        assert detail.subject == "Прочитать меня"
        att, data = client.get_attachment("7", detail.attachments[0].index)
        assert data == b"pdf-bytes"


def test_imap_get_message_not_found_raises_mail_error(monkeypatch):
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: FakeImapConn({}))
    with mail_client.get_incoming_client(_imap_settings()) as client:
        with pytest.raises(MailError):
            client.get_message("999")


# ---------------------------------------------------------------------------
# POP3
# ---------------------------------------------------------------------------

class FakePop3Conn:
    def __init__(self, messages: list[bytes], supports_top: bool = True):
        self._messages = messages  # индекс 0 -> номер 1
        self._supports_top = supports_top

    def user(self, name):
        pass

    def pass_(self, password):
        pass

    def stat(self):
        return (len(self._messages), 0)

    def top(self, num, lines):
        if not self._supports_top:
            import poplib
            raise poplib.error_proto("ERR unsupported")
        raw = self._messages[num - 1]
        return (b"+OK", raw.split(b"\r\n"), len(raw))

    def retr(self, num):
        raw = self._messages[num - 1]
        return (b"+OK", raw.split(b"\r\n"), len(raw))

    def quit(self):
        pass


def _pop3_settings():
    return MailboxSettings(
        incoming_protocol=MailProtocol.POP3, incoming_host="pop.example.com", incoming_port=995,
        incoming_encryption=MailEncryption.SSL, smtp_host="smtp.example.com", smtp_port=587,
        smtp_encryption=MailEncryption.STARTTLS, username="pravlenie@example.com", password_encrypted="",
    )


def test_pop3_list_messages_with_top(monkeypatch):
    raws = [_make_test_email(subject=f"POP {i}", with_inline_image=False, with_attachment=False).as_bytes() for i in range(1, 4)]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        assert client.supports_folders is False
        assert client.supports_flags is False
        page = client.list_messages(page=1, page_size=25)
        assert page.total == 3
        assert [m.uid for m in page.messages] == ["3", "2", "1"]
        assert all(m.seen is None for m in page.messages)


def test_pop3_list_messages_falls_back_without_top(monkeypatch):
    raws = [_make_test_email(subject="Без TOP", with_inline_image=False, with_attachment=False).as_bytes()]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws, supports_top=False))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        page = client.list_messages(page=1, page_size=25)
        assert page.total == 1
        assert page.messages[0].subject == "Без TOP"


def test_pop3_get_message(monkeypatch):
    raws = [_make_test_email(subject="Целиком").as_bytes()]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        detail = client.get_message("1")
        assert detail.subject == "Целиком"


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

class FakeSmtpConn:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def _smtp_settings():
    return MailboxSettings(
        incoming_protocol=MailProtocol.IMAP, incoming_host="imap.example.com", incoming_port=993,
        incoming_encryption=MailEncryption.SSL, smtp_host="smtp.example.com", smtp_port=587,
        smtp_encryption=MailEncryption.STARTTLS, username="pravlenie@example.com", password_encrypted="",
        from_name="Правление ГСК",
    )


def test_send_message_builds_correct_email(monkeypatch):
    fake = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake)

    mail_client.send_message(
        _smtp_settings(), to_addrs=["a@example.com", "b@example.com"],
        subject="Повестка собрания", body_text="Текст письма",
    )
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["Subject"] == "Повестка собрания"
    assert "a@example.com" in sent["To"] and "b@example.com" in sent["To"]
    assert "Правление ГСК" in sent["From"]
    assert "pravlenie@example.com" in sent["From"]


def test_send_message_with_attachment(monkeypatch):
    fake = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake)

    mail_client.send_message(
        _smtp_settings(), to_addrs=["a@example.com"], subject="С вложением", body_text="Текст",
        attachments=[("report.pdf", "application/pdf", b"pdf-data")],
    )
    sent = fake.sent[0]
    atts = list(sent.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "report.pdf"
    assert atts[0].get_payload(decode=True) == b"pdf-data"


def test_send_message_smtp_failure_raises_mail_error(monkeypatch):
    import smtplib

    class FailingSmtpConn:
        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no such user")})
        def quit(self):
            pass

    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: FailingSmtpConn())
    with pytest.raises(MailError):
        mail_client.send_message(_smtp_settings(), to_addrs=["a@example.com"], subject="x", body_text="y")


# ---------------------------------------------------------------------------
# Обёртка ошибок подключения (нужен app-контекст ради crypto.decrypt)
# ---------------------------------------------------------------------------

def test_connect_imap_wraps_connection_error_as_mail_error(app, monkeypatch):
    import imaplib

    def boom(*a, **kw):
        raise OSError("Connection refused")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", boom)
    with app.app_context():
        with pytest.raises(MailError):
            mail_client._connect_imap(_imap_settings())
