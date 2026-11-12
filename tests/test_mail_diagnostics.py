"""
Пошаговая проверка подключения к почте (app/mail_diagnostics.py) и роут
mailbox.test_connection в обоих режимах: поток NDJSON для окна на странице
и прежний POST → flash без JS. Сеть и протоколы подменены; рукопожатие TLS
проверяется на настоящем локальном сокете без SSL.
"""
import imaplib
import json
import socket
import threading

import pytest

from app import database, mail_client, mail_diagnostics
from app.bank_api import crypto
from app.models import MailboxSettings, MailEncryption, MailProtocol, RoleEnum
from tests.conftest import login, make_person, make_user


def _settings(**overrides):
    values = dict(
        incoming_protocol=MailProtocol.IMAP, incoming_host="imap.example.com", incoming_port=143,
        incoming_encryption=MailEncryption.STARTTLS, smtp_host="smtp.example.com", smtp_port=587,
        smtp_encryption=MailEncryption.STARTTLS, username="pravlenie@example.com",
        password_encrypted=crypto.encrypt("secret123"), sent_folder="INBOX.Sent",
    )
    values.update(overrides)
    return MailboxSettings(**values)


class FakeImap:
    error = imaplib.IMAP4.error  # как у настоящего: модуль ловит imaplib.IMAP4.error
    folders = {"INBOX": b"36", "INBOX.Sent": b"15"}
    login_error = None

    def __init__(self, host, port, timeout=None):
        self.welcome = b"* OK IMAP ready"

    def starttls(self):
        pass

    def login(self, user, password):
        if self.login_error:
            raise self.error(self.login_error)

    def select(self, name, readonly=False):
        assert readonly, "проверка не должна открывать папки на запись"
        name = name.strip('"')
        if name in self.folders:
            return "OK", [self.folders[name]]
        return "NO", [b"Mailbox doesn't exist"]

    def logout(self):
        pass


class FakeSmtp:
    def __init__(self, host="", port=0, timeout=None):
        self.ehlo_resp = None

    def ehlo(self):
        self.ehlo_resp = b"smtp.example.com Hello\nAUTH PLAIN LOGIN"

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def quit(self):
        pass


class _Sock:
    def close(self):
        pass


@pytest.fixture
def ctx(app):
    """Пароль в настройках шифруется ключом приложения."""
    with app.app_context():
        yield


@pytest.fixture
def network(monkeypatch):
    """DNS и TCP успешны; tcp_errors — {порт: исключение} для отказа."""
    state = {"tcp_errors": {}, "dns_error": None}

    def getaddrinfo(host, port, *args, **kwargs):
        if state["dns_error"]:
            raise state["dns_error"]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", port))]

    def create_connection(address, timeout=None):
        error = state["tcp_errors"].get(address[1])
        if error:
            raise error
        return _Sock()

    monkeypatch.setattr(mail_diagnostics.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(mail_diagnostics.socket, "create_connection", create_connection)
    monkeypatch.setattr(mail_diagnostics.imaplib, "IMAP4", FakeImap)
    monkeypatch.setattr(mail_diagnostics.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(FakeImap, "login_error", None)
    return state


def _final(events):
    """Последнее состояние каждого шага: {(группа, шаг): событие}."""
    steps = {}
    for ev in events:
        if ev["type"] == "step":
            steps[(ev["group"], ev["id"])] = ev
    return steps


def test_all_steps_ok_in_order_with_running_first(ctx, network):
    events = list(mail_diagnostics.run(_settings()))
    order = [(e["group"], e["id"], e["status"]) for e in events if e["type"] == "step"]
    assert order[:2] == [("incoming", "dns", "running"), ("incoming", "dns", "ok")]
    assert [s[1] for s in order if s[2] != "running"] == [
        "dns", "tcp", "greeting", "login", "folders", "dns", "tcp", "greeting", "login"]
    assert events[-1] == {"type": "done", "ok": True, "errors": []}
    final = _final(events)
    assert "192.0.2.10" in final[("incoming", "dns")]["detail"]
    assert final[("smtp", "greeting")]["detail"] == "smtp.example.com Hello, STARTTLS выполнен"
    assert final[("incoming", "folders")]["detail"] == "писем — Входящие: 36, Отправленные: 15"
    assert events[0]["group_title"] == "Входящая почта — IMAP imap.example.com:143"


def test_smtp_timeout_points_to_hosting_and_skips_rest(ctx, network):
    network["tcp_errors"][587] = TimeoutError("timed out")
    events = list(mail_diagnostics.run(_settings()))
    final = _final(events)
    tcp = final[("smtp", "tcp")]
    assert tcp["status"] == "fail"
    assert tcp["detail"] == "порт 587 не ответил за 15 с"
    assert "хостинги VPS блокируют SMTP" in tcp["hint"]
    assert final[("smtp", "greeting")]["status"] == "skip"
    assert final[("smtp", "login")]["status"] == "skip"
    assert final[("incoming", "login")]["status"] == "ok"
    assert events[-1]["errors"] == ["SMTP: подключение к порту: порт 587 не ответил за 15 с"]


def test_incoming_timeout_hint_is_not_about_smtp(ctx, network):
    network["tcp_errors"][143] = TimeoutError("timed out")
    final = _final(mail_diagnostics.run(_settings()))
    assert "SMTP" not in final[("incoming", "tcp")]["hint"]


def test_dns_failure(ctx, network):
    network["dns_error"] = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    final = _final(mail_diagnostics.run(_settings()))
    dns = final[("incoming", "dns")]
    assert dns["status"] == "fail"
    assert "imap.example.com не найден" in dns["detail"]
    assert final[("incoming", "tcp")]["status"] == "skip"


def test_refused_port_lists_usual_ports(ctx, network):
    network["tcp_errors"][587] = ConnectionRefusedError()
    tcp = _final(mail_diagnostics.run(_settings()))[("smtp", "tcp")]
    assert "отказал" in tcp["detail"]
    assert "465 (SSL) или 587 (STARTTLS)" in tcp["hint"]


def test_login_rejected(ctx, network, monkeypatch):
    monkeypatch.setattr(FakeImap, "login_error", b"[AUTHENTICATIONFAILED] Invalid credentials")
    events = list(mail_diagnostics.run(_settings()))
    login_step = _final(events)[("incoming", "login")]
    assert login_step["status"] == "fail"
    assert login_step["detail"] == "сервер отклонил вход: [AUTHENTICATIONFAILED] Invalid credentials"
    assert "пароль" in login_step["hint"]
    assert _final(events)[("incoming", "folders")]["status"] == "skip"
    assert events[-1]["ok"] is False


def test_missing_folder_is_warning_not_error(ctx, network):
    events = list(mail_diagnostics.run(_settings(trash_folder="Trash")))
    folders = _final(events)[("incoming", "folders")]
    assert folders["status"] == "warn"
    assert "Корзина (Trash)" in folders["detail"]
    assert events[-1]["ok"] is True


def test_unexpected_error_is_shown_not_raised(ctx, network, monkeypatch):
    def broken(self):
        raise RuntimeError("что-то странное")

    monkeypatch.setattr(FakeSmtp, "ehlo", broken)
    events = list(mail_diagnostics.run(_settings()))
    greeting = _final(events)[("smtp", "greeting")]
    assert greeting["status"] == "fail"
    assert "RuntimeError" in greeting["detail"]


def test_tls_step_on_plain_port_says_no_ssl():
    """Настоящий сокет: сервер отвечает обычным SMTP-приветствием, а клиент
    ждёт SSL — рукопожатие падает, подсказка про STARTTLS."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _addr = server.accept()
        conn.sendall(b"220 plain smtp\r\n")
        conn.recv(1024)
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(mail_diagnostics.StepFailed) as info:
            mail_diagnostics._tls("127.0.0.1", port)
    finally:
        server.close()
        thread.join(timeout=5)
    assert "рукопожатие TLS не удалось" in info.value.detail
    assert "STARTTLS" in info.value.hint


# --- роут ------------------------------------------------------------------------

def _chairman_with_settings(db, client):
    person = make_person(db, full_name="Chairman One")
    make_user(db, "chair1", "pass1234", role=RoleEnum.CHAIRMAN, person=person)
    db.add(_settings())
    db.commit()
    login(client, "chair1", "pass1234")


FAKE_EVENTS = [
    {"type": "step", "group": "smtp", "group_title": "SMTP", "id": "tcp", "title": "Подключение к порту",
     "status": "running", "detail": "", "hint": ""},
    {"type": "step", "group": "smtp", "group_title": "SMTP", "id": "tcp", "title": "Подключение к порту",
     "status": "fail", "detail": "порт 587 не ответил за 15 с", "hint": "…"},
    {"type": "done", "ok": False, "errors": ["SMTP: подключение к порту: порт 587 не ответил за 15 с"]},
]


def test_route_streams_ndjson_and_saves_last_error(db, client, monkeypatch):
    _chairman_with_settings(db, client)
    monkeypatch.setattr(mail_diagnostics, "run", lambda settings: iter(FAKE_EVENTS))

    resp = client.post("/mailbox/settings/test-connection", headers={"Accept": "application/x-ndjson"})
    assert resp.status_code == 200
    assert resp.mimetype == "application/x-ndjson"
    assert resp.headers["X-Accel-Buffering"] == "no"
    lines = [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]
    assert [line["type"] for line in lines] == ["step", "step", "done"]
    assert lines[-1]["last_error"] == "SMTP: подключение к порту: порт 587 не ответил за 15 с"

    database.db_session.remove()
    assert db.query(MailboxSettings).first().last_error == lines[-1]["last_error"]


def test_route_clears_last_error_on_success(db, client, monkeypatch):
    _chairman_with_settings(db, client)
    setting = db.query(MailboxSettings).first()
    setting.last_error = "старая ошибка"
    db.commit()
    monkeypatch.setattr(mail_diagnostics, "run", lambda settings: iter([{"type": "done", "ok": True, "errors": []}]))

    resp = client.post("/mailbox/settings/test-connection", headers={"Accept": "application/x-ndjson"})
    assert json.loads(resp.get_data(as_text=True).splitlines()[-1])["last_error"] is None
    database.db_session.remove()
    assert db.query(MailboxSettings).first().last_error is None


def test_route_without_js_flashes_result(db, client, monkeypatch):
    _chairman_with_settings(db, client)
    monkeypatch.setattr(mail_diagnostics, "run", lambda settings: iter(FAKE_EVENTS))
    # Страница почты после редиректа читает ящик — без сети.
    def no_network(settings):
        raise mail_client.MailError("IMAP: нет сети")

    monkeypatch.setattr(mail_client, "_connect_imap", no_network)

    resp = client.post("/mailbox/settings/test-connection")
    assert resp.status_code == 302
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Ошибка подключения: SMTP: подключение к порту" in page


def test_route_not_configured_streams_message(db, client):
    person = make_person(db, full_name="Chairman One")
    make_user(db, "chair1", "pass1234", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()
    login(client, "chair1", "pass1234")

    resp = client.post("/mailbox/settings/test-connection", headers={"Accept": "application/x-ndjson"})
    [line] = [json.loads(x) for x in resp.get_data(as_text=True).splitlines()]
    assert line["ok"] is False
    assert "заполните" in line["errors"][0]
