"""scripts/backup_mailbox.py — архив почты правления в .eml (IMAP/POP3
подменены фейковыми соединениями)."""
import importlib.util
import os

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_spec = importlib.util.spec_from_file_location("backup_mailbox_under_test", os.path.join(SCRIPTS_DIR, "backup_mailbox.py"))
backup_mailbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_mailbox)


@pytest.mark.parametrize("line, expected", [
    (b'(\\HasNoChildren) "/" "INBOX"', "INBOX"),
    (b'(\\HasNoChildren \\Sent) "/" Sent', "Sent"),
    (b'(\\HasChildren) "." "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"', "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"),
    (b'(\\Noselect \\HasChildren) "/" "[Gmail]"', None),
    (b'(\\HasNoChildren) "/" "with \\"quote\\""', 'with "quote"'),
    (b"garbage", None),
])
def test_parse_list_line(line, expected):
    assert backup_mailbox.parse_list_line(line) == expected


def test_safe_name_keeps_plain_and_disambiguates_replaced():
    assert backup_mailbox.safe_name("INBOX") == "INBOX"
    a, b = backup_mailbox.safe_name("a/b"), backup_mailbox.safe_name("a:b")
    assert "/" not in a and a != b
    assert backup_mailbox.safe_name("..") not in ("", ".", "..")
    assert len(backup_mailbox.safe_name("x" * 300)) <= 100


class FakeImap:
    def __init__(self, folders):
        self.folders = folders  # имя → (uidvalidity, {uid: raw})
        self.selected = None
        self.fetched = []
        self.readonly = None

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        self.readonly = readonly
        return ("OK", [b"1"]) if self.selected in self.folders else ("NO", [b"no such folder"])

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [str(self.folders[self.selected][0]).encode()]

    def uid(self, command, *args):
        messages = self.folders[self.selected][1]
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in messages).encode()]
        assert command == "FETCH" and args[1] == "(UID BODY.PEEK[])"
        uids = [int(u) for u in args[0].split(",")]
        self.fetched.extend(uids)
        data = []
        for n, uid in enumerate(uids, 1):
            data.append((f"{n} (UID {uid} BODY[] {{{len(messages[uid])}}}".encode(), messages[uid]))
            data.append(b")")
        return "OK", data

    def logout(self):
        pass


def test_imap_downloads_only_new_messages_read_only(tmp_path, monkeypatch):
    conn = FakeImap({"INBOX": (7, {1: b"Subject: a\r\n\r\nA", 2: b"Subject: b\r\n\r\nB"}), "Sent": (3, {5: b"S"})})
    import app.mail_client
    monkeypatch.setattr(app.mail_client, "_connect_imap", lambda settings: conn)

    message = backup_mailbox.backup_imap(object(), str(tmp_path))
    assert message == "IMAP: папок 2, новых писем 3"
    assert conn.readonly is True
    assert (tmp_path / "imap" / "INBOX" / "7" / "2.eml").read_bytes() == b"Subject: b\r\n\r\nB"
    assert (tmp_path / "imap" / "Sent" / "3" / "5.eml").read_bytes() == b"S"

    conn.folders["INBOX"][1][3] = b"new"
    conn.fetched.clear()
    assert backup_mailbox.backup_imap(object(), str(tmp_path)) == "IMAP: папок 2, новых писем 1"
    assert conn.fetched == [3]


def test_imap_new_uidvalidity_goes_to_new_directory(tmp_path, monkeypatch):
    conn = FakeImap({"INBOX": (7, {1: b"old"})})
    import app.mail_client
    monkeypatch.setattr(app.mail_client, "_connect_imap", lambda settings: conn)
    backup_mailbox.backup_imap(object(), str(tmp_path))
    conn.folders["INBOX"] = (8, {1: b"renumbered"})
    backup_mailbox.backup_imap(object(), str(tmp_path))
    assert (tmp_path / "imap" / "INBOX" / "7" / "1.eml").read_bytes() == b"old"
    assert (tmp_path / "imap" / "INBOX" / "8" / "1.eml").read_bytes() == b"renumbered"


class FakePop3:
    def __init__(self, messages):
        self.messages = messages  # [(uidl, [lines])]
        self.retrieved = []

    def uidl(self):
        return b"+OK", [f"{n} {uidl}".encode() for n, (uidl, _) in enumerate(self.messages, 1)], 0

    def retr(self, number):
        self.retrieved.append(number)
        return b"+OK", self.messages[number - 1][1], 0

    def quit(self):
        pass


def test_pop3_by_uidl(tmp_path, monkeypatch):
    conn = FakePop3([("u1", [b"Subject: a", b"", b"A"]), ("u/2", [b"B"])])
    import app.mail_client
    monkeypatch.setattr(app.mail_client, "_connect_pop3", lambda settings: conn)
    assert backup_mailbox.backup_pop3(object(), str(tmp_path)) == "POP3: писем на сервере 2, новых 2"
    assert (tmp_path / "pop3" / "u1.eml").read_bytes() == b"Subject: a\r\n\r\nA\r\n"
    conn.retrieved.clear()
    assert backup_mailbox.backup_pop3(object(), str(tmp_path)) == "POP3: писем на сервере 2, новых 0"
    assert conn.retrieved == []
