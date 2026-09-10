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
import re
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


def test_decode_idn_address_decodes_punycode_domain():
    assert mail_client.decode_idn_address("pravlenie@xn----dtbbg1boax0b.xn--p1ai") == "pravlenie@гм-восход.рф"


def test_decode_idn_address_leaves_ascii_domain_as_is():
    assert mail_client.decode_idn_address("member@example.com") == "member@example.com"


def test_decode_idn_address_handles_missing_or_malformed_input():
    assert mail_client.decode_idn_address(None) == ""
    assert mail_client.decode_idn_address("") == ""
    assert mail_client.decode_idn_address("не-адрес-без-собаки") == "не-адрес-без-собаки"


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

    def __init__(self, messages: dict[int, bytes], folders: dict[str, dict] | None = None):
        self._messages = messages  # uid -> raw bytes (текущая выбранная папка)
        self._flags: dict[int, set[str]] = {}  # uid -> набор IMAP-флагов (\Seen, \Flagged, ...)
        self._has_attachment: dict[int, bool] = {}  # uid -> есть ли disposition "attachment" в BODYSTRUCTURE
        self.selected_folder = None
        self.expunged = False
        # Для теста сохранения в "Отправленные"/перемещения в "Корзину":
        # folders — доп. папки вида {"Sent": {"exists": True/False, "appended": [...]}}
        self.folders = folders if folders is not None else {}

    def login(self, user, password):
        return ("OK", [b"logged in"])

    def select(self, folder):
        self.selected_folder = folder.strip('"')
        return ("OK", [b"1"])

    def append(self, mailbox, flags, date_time, message):
        name = mailbox.strip('"')
        info = self.folders.setdefault(name, {"exists": True, "appended": []})
        if not info.get("exists", True):
            return ("NO", [b"[TRYCREATE] No such mailbox"])
        info["appended"].append(message)
        return ("OK", [b"APPEND completed"])

    def create(self, mailbox):
        name = mailbox.strip('"')
        self.folders.setdefault(name, {"exists": True, "appended": []})["exists"] = True
        return ("OK", [b"CREATE completed"])

    def expunge(self):
        self.expunged = True
        return ("OK", [b""])

    def uid(self, command, *args):
        if command == "search":
            criteria = args[-1] if args else "ALL"
            if criteria == "UNSEEN":
                matching = [u for u in self._messages if "\\Seen" not in self._flags.get(u, set())]
            else:
                matching = list(self._messages)
            uids = " ".join(str(u) for u in sorted(matching)).encode()
            return ("OK", [uids])
        if command == "store":
            uid = int(args[0].decode() if isinstance(args[0], bytes) else args[0])
            mode = args[1]
            flag_names = re.findall(r"\\\w+", args[2])
            current = self._flags.setdefault(uid, set())
            if mode == "+FLAGS":
                current.update(flag_names)
            elif mode == "-FLAGS":
                current.difference_update(flag_names)
            if "\\Deleted" in current:
                self._messages.pop(uid, None)
            return ("OK", [("FLAGS (" + " ".join(sorted(current)) + ")").encode()])
        if command == "copy":
            uid = int(args[0].decode() if isinstance(args[0], bytes) else args[0])
            name = args[1].strip('"')
            raw = self._messages.get(uid)
            if raw is None:
                return ("NO", [None])
            info = self.folders.setdefault(name, {"exists": True, "appended": []})
            if not info.get("exists", True):
                return ("NO", [b"[TRYCREATE] No such mailbox"])
            info["appended"].append(raw)
            return ("OK", [b"COPY completed"])
        if command == "fetch":
            uid = int(args[0].decode() if isinstance(args[0], bytes) else args[0])
            spec = args[1] if len(args) > 1 else ""
            raw = self._messages.get(uid)
            if raw is None:
                return ("NO", [None])
            if "RFC822" in spec:
                return ("OK", [(f"{uid} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw)])
            flags_str = " ".join(sorted(self._flags.get(uid, set())))
            bodystructure = '("attachment")' if self._has_attachment.get(uid) else "()"
            meta = f"{uid} (UID {uid} FLAGS ({flags_str}) BODYSTRUCTURE {bodystructure} BODY[HEADER.FIELDS (SUBJECT FROM DATE)] {{999}}".encode()
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


def test_imap_delete_message_marks_deleted_and_expunges(monkeypatch):
    msgs = {5: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()}
    fake = FakeImapConn(msgs)
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.delete_message("5")

    assert fake.expunged is True
    assert 5 not in msgs


def test_imap_list_messages_selects_requested_folder(monkeypatch):
    fake = FakeImapConn({})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.list_messages(page=1, folder="Sent")

    assert fake.selected_folder == "Sent"


def test_imap_count_unread_counts_only_unseen(monkeypatch):
    fake = FakeImapConn({
        1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes(),
        2: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes(),
        3: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes(),
    })
    fake._flags[1] = {"\\Seen"}
    fake._flags[2] = {"\\Seen"}
    # 3 остаётся непрочитанным
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        assert client.count_unread() == 1


def test_imap_count_unread_zero_when_all_read(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    fake._flags[1] = {"\\Seen"}
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        assert client.count_unread() == 0


def test_pop3_count_unread_raises(monkeypatch):
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn([]))
    with mail_client.get_incoming_client(_pop3_settings()) as client:
        with pytest.raises(MailError):
            client.count_unread()


def test_imap_list_messages_reports_flagged(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    fake._flags[1] = {"\\Seen", "\\Flagged"}
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1)
    assert page.messages[0].seen is True
    assert page.messages[0].flagged is True


def test_imap_list_messages_reports_has_attachments(monkeypatch):
    fake = FakeImapConn({
        1: _make_test_email(with_inline_image=False, with_attachment=True).as_bytes(),
        2: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes(),
    })
    fake._has_attachment[1] = True
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, sort="date", sort_dir="asc")
    by_uid = {m.uid: m.has_attachments for m in page.messages}
    assert by_uid == {"1": True, "2": False}


def _email(subject, from_addr, to_addr="member@example.com", date_str="Fri, 04 Sep 2026 12:00:00 +0300"):
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = date_str
    msg.set_content("Текст")
    return msg


def test_imap_list_messages_search_matches_subject(monkeypatch):
    fake = FakeImapConn({
        1: _email("Собрание правления", "a@example.com").as_bytes(),
        2: _email("Счёт на оплату", "b@example.com").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, search="оплату")
    assert [m.subject for m in page.messages] == ["Счёт на оплату"]
    assert page.total == 1


def test_imap_list_messages_search_matches_from_case_insensitively(monkeypatch):
    fake = FakeImapConn({
        1: _email("Тема 1", "Иванов И.И. <ivanov@example.com>").as_bytes(),
        2: _email("Тема 2", "Петров П.П. <petrov@example.com>").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, search="ИВАНОВ")
    assert [m.subject for m in page.messages] == ["Тема 1"]


def test_imap_list_messages_search_matches_to_address(monkeypatch):
    fake = FakeImapConn({
        1: _email("Тема 1", "a@example.com", to_addr="board@example.com").as_bytes(),
        2: _email("Тема 2", "b@example.com", to_addr="other@example.com").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, search="board@")
    assert [m.subject for m in page.messages] == ["Тема 1"]


def test_imap_list_messages_search_no_match_returns_empty(monkeypatch):
    fake = FakeImapConn({1: _email("Тема", "a@example.com").as_bytes()})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, search="нет такого")
    assert page.messages == []
    assert page.total == 0


def test_imap_list_messages_sort_by_subject_ascending(monkeypatch):
    fake = FakeImapConn({
        1: _email("Яблоко", "a@example.com").as_bytes(),
        2: _email("Апельсин", "b@example.com").as_bytes(),
        3: _email("Банан", "c@example.com").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, sort="subject", sort_dir="asc")
    assert [m.subject for m in page.messages] == ["Апельсин", "Банан", "Яблоко"]


def test_imap_list_messages_sort_by_from_descending(monkeypatch):
    fake = FakeImapConn({
        1: _email("Тема 1", "Аня <anya@example.com>").as_bytes(),
        2: _email("Тема 2", "Борис <boris@example.com>").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, sort="from", sort_dir="desc")
    assert [m.from_name for m in page.messages] == ["Борис", "Аня"]


def test_imap_list_messages_sort_by_date_ascending(monkeypatch):
    fake = FakeImapConn({
        1: _email("Новое", "a@example.com", date_str="Sun, 06 Sep 2026 12:00:00 +0300").as_bytes(),
        2: _email("Старое", "b@example.com", date_str="Mon, 01 Sep 2026 12:00:00 +0300").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1, sort="date", sort_dir="asc")
    assert [m.subject for m in page.messages] == ["Старое", "Новое"]


def test_imap_list_messages_default_sort_is_date_descending(monkeypatch):
    """Без явной сортировки поведение как раньше — новые письма первыми."""
    fake = FakeImapConn({
        1: _email("Старое", "a@example.com", date_str="Mon, 01 Sep 2026 12:00:00 +0300").as_bytes(),
        2: _email("Новое", "b@example.com", date_str="Sun, 06 Sep 2026 12:00:00 +0300").as_bytes(),
    })
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        page = client.list_messages(page=1)
    assert [m.subject for m in page.messages] == ["Новое", "Старое"]


def test_imap_set_state_unread_clears_seen_and_flagged(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    fake._flags[1] = {"\\Seen", "\\Flagged"}
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.set_state("1", mail_client.STATE_UNREAD)
    assert fake._flags[1] == set()


def test_imap_set_state_read_sets_seen_and_clears_flagged(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    fake._flags[1] = {"\\Flagged"}
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.set_state("1", mail_client.STATE_READ)
    assert fake._flags[1] == {"\\Seen"}


def test_imap_set_state_important_adds_flagged_without_touching_seen(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.set_state("1", mail_client.STATE_IMPORTANT)
    assert fake._flags[1] == {"\\Flagged"}


def test_imap_set_state_unknown_raises(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        with pytest.raises(MailError):
            client.set_state("1", "bogus")


def test_imap_move_message_copies_to_target_and_removes_from_source(monkeypatch):
    fake = FakeImapConn({1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.move_message("1", "INBOX", "Trash")

    assert len(fake.folders["Trash"]["appended"]) == 1
    assert 1 not in fake._messages
    assert fake.expunged is True


def test_imap_move_message_creates_target_folder_if_missing(monkeypatch):
    fake = FakeImapConn(
        {1: _make_test_email(with_inline_image=False, with_attachment=False).as_bytes()},
        folders={"Trash": {"exists": False, "appended": []}},
    )
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake)

    with mail_client.get_incoming_client(_imap_settings()) as client:
        client.move_message("1", "INBOX", "Trash")

    assert fake.folders["Trash"]["exists"] is True
    assert len(fake.folders["Trash"]["appended"]) == 1


def test_pop3_set_state_and_move_message_raise_not_supported(monkeypatch):
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn([]))
    with mail_client.get_incoming_client(_pop3_settings()) as client:
        with pytest.raises(MailError):
            client.set_state("1", mail_client.STATE_READ)
        with pytest.raises(MailError):
            client.move_message("1", "INBOX", "Trash")


def test_send_message_saves_copy_to_sent_folder(monkeypatch):
    fake_imap = FakeImapConn({})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake_imap)
    fake_smtp = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake_smtp)

    settings = _imap_settings()
    settings.sent_folder = "Sent"
    mail_client.send_message(settings, to_addrs=["a@example.com"], subject="Тест", body_text="Текст")

    assert "Sent" in fake_imap.folders
    assert len(fake_imap.folders["Sent"]["appended"]) == 1


def test_send_message_creates_sent_folder_if_missing(monkeypatch):
    fake_imap = FakeImapConn({}, folders={"Sent": {"exists": False, "appended": []}})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake_imap)
    fake_smtp = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake_smtp)

    settings = _imap_settings()
    settings.sent_folder = "Sent"
    mail_client.send_message(settings, to_addrs=["a@example.com"], subject="Тест", body_text="Текст")

    assert fake_imap.folders["Sent"]["exists"] is True
    assert len(fake_imap.folders["Sent"]["appended"]) == 1


def test_send_message_without_sent_folder_configured_does_not_append(monkeypatch):
    fake_imap = FakeImapConn({})
    monkeypatch.setattr(mail_client, "_connect_imap", lambda settings: fake_imap)
    fake_smtp = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake_smtp)

    settings = _imap_settings()
    settings.sent_folder = None
    mail_client.send_message(settings, to_addrs=["a@example.com"], subject="Тест", body_text="Текст")

    assert fake_imap.folders == {}


def test_send_message_sent_folder_failure_does_not_fail_send(monkeypatch):
    """Best-effort: копия в "Отправленные" не сохранилась — письмо всё
    равно считается успешно отправленным (получателю оно уже ушло)."""
    def boom_connect(settings):
        raise mail_client.MailError("IMAP unreachable")

    monkeypatch.setattr(mail_client, "_connect_imap", boom_connect)
    fake_smtp = FakeSmtpConn()
    monkeypatch.setattr(mail_client, "_connect_smtp", lambda settings: fake_smtp)

    settings = _imap_settings()
    settings.sent_folder = "Sent"
    mail_client.send_message(settings, to_addrs=["a@example.com"], subject="Тест", body_text="Текст")
    assert len(fake_smtp.sent) == 1


# ---------------------------------------------------------------------------
# POP3
# ---------------------------------------------------------------------------

class FakePop3Conn:
    def __init__(self, messages: list[bytes], supports_top: bool = True, supports_uidl: bool = True, uidls: list[str] | None = None):
        self._messages = messages  # индекс 0 -> номер 1
        self._supports_top = supports_top
        self._supports_uidl = supports_uidl
        self._uidls = uidls  # индекс 0 -> uidl номера 1, по умолчанию "uidl-<num>"
        self.deleted = []

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

    def uidl(self):
        if not self._supports_uidl:
            import poplib
            raise poplib.error_proto("ERR unsupported")
        lines = []
        for i in range(len(self._messages)):
            num = i + 1
            uidl = self._uidls[i] if self._uidls else f"uidl-{num}"
            lines.append(f"{num} {uidl}".encode())
        return (b"+OK", lines, 0)

    def dele(self, num):
        self.deleted.append(num)

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
        assert all(m.has_attachments is None for m in page.messages)
        assert [m.uidl for m in page.messages] == ["uidl-3", "uidl-2", "uidl-1"]
        assert client.supports_message_state is True


def test_pop3_get_uidl_map(monkeypatch):
    raws = [_make_test_email(with_inline_image=False, with_attachment=False).as_bytes() for _ in range(2)]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws, uidls=["abc", "def"]))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        assert client.get_uidl_map() == {1: "abc", 2: "def"}


def test_pop3_list_current_uidls(monkeypatch):
    raws = [_make_test_email(with_inline_image=False, with_attachment=False).as_bytes() for _ in range(2)]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws, uidls=["abc", "def"]))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        assert client.list_current_uidls() == {"abc", "def"}


def test_pop3_get_uidl_map_none_when_unsupported(monkeypatch):
    raws = [_make_test_email(with_inline_image=False, with_attachment=False).as_bytes()]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws, supports_uidl=False))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        assert client.get_uidl_map() is None
        assert client.list_current_uidls() is None


def test_pop3_list_messages_uidl_none_when_unsupported(monkeypatch):
    raws = [_make_test_email(with_inline_image=False, with_attachment=False).as_bytes()]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws, supports_uidl=False))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        page = client.list_messages(page=1)
        assert page.messages[0].uidl is None
        assert client.supports_message_state is False


def test_pop3_list_messages_search_and_sort(monkeypatch):
    raws = [_email("Яблоко", "a@example.com").as_bytes(), _email("Апельсин", "b@example.com").as_bytes()]
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: FakePop3Conn(raws))

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        page = client.list_messages(page=1, sort="subject", sort_dir="asc")
    assert [m.subject for m in page.messages] == ["Апельсин", "Яблоко"]

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        page = client.list_messages(page=1, search="яблоко")
    assert [m.subject for m in page.messages] == ["Яблоко"]


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


def test_pop3_delete_message_calls_dele(monkeypatch):
    raws = [_make_test_email(with_inline_image=False, with_attachment=False).as_bytes()]
    fake = FakePop3Conn(raws)
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: fake)

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        client.delete_message("1")

    assert fake.deleted == [1]


def test_pop3_rejects_non_inbox_folder(monkeypatch):
    fake = FakePop3Conn([])
    monkeypatch.setattr(mail_client, "_connect_pop3", lambda settings: fake)

    with mail_client.get_incoming_client(_pop3_settings()) as client:
        with pytest.raises(MailError):
            client.list_messages(page=1, folder="Sent")
        with pytest.raises(MailError):
            client.delete_message("1", folder="Sent")


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


def test_non_ascii_login_raises_mail_error_not_raw_unicode_error(app, monkeypatch):
    """Регресс: imaplib/poplib/smtplib кодируют команды протокола в ASCII —
    нелатинский логин/пароль (например кириллический домен почты) роняет
    login() с сырым UnicodeEncodeError (ПОДКЛАСС ValueError). Без явного
    except UnicodeError в _connect_* это проскакивало мимо MailError и
    попадало в общий обработчик форм (app/errors.py: ValueError на уровне
    приложения) — пользователь видел бесполезное "проверьте правильность
    заполнения формы" вместо объяснения, что именно не так, а страница
    почты вообще переставала открываться."""
    class FakeConnBadLogin:
        def login(self, user, password):
            raise UnicodeEncodeError("ascii", user, 0, 1, "ordinal not in range(128)")

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: FakeConnBadLogin())

    settings = _imap_settings()
    settings.username = "логин@пример.рф"

    with app.app_context():
        with pytest.raises(MailError) as exc_info:
            mail_client._connect_imap(settings)
        assert "латиницу" in str(exc_info.value)
