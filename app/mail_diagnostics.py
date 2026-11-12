"""
Пошаговая проверка подключения к почте правления — кнопка «Проверить
подключение» (mailbox.test_connection). Вместо одного «SMTP: timed out»
видно, на чём именно споткнулось:

  входящая (IMAP/POP3) и исходящая (SMTP), для каждой по порядку:
    DNS          — адрес сервера находится;
    TCP          — порт отвечает (отдельно от протокола: таймаут здесь —
                   файрвол/провайдер, а не логин);
    TLS          — только для SSL: рукопожатие и проверка сертификата;
    приветствие  — сервер говорит на этом протоколе (+ STARTTLS);
    вход         — логин и пароль;
    папки (IMAP) — INBOX и заданные в настройках папки открываются.

run() — генератор: перед шагом отдаёт его со статусом running, после — с
ok / warn / fail и пояснением; после неудачи остальные шаги этой стороны
приходят со статусом skip. Роут стримит события в браузер (NDJSON), так что
галочки появляются по мере проверки. Ничего не меняет на сервере почты:
IMAP-папки открываются через EXAMINE (только чтение).

Сообщения — по-русски без _(), как и MailError в mail_client.
"""
from __future__ import annotations

import imaplib
import poplib
import smtplib
import socket
import ssl
import time
from collections.abc import Callable, Iterator

from .mail_client import CONNECT_TIMEOUT, _connection_error_message, _decrypted_password
from .models import MailboxSettings, MailEncryption, MailProtocol

OK, WARN, FAIL, SKIP, RUNNING = "ok", "warn", "fail", "skip", "running"

PORTS_HINT = ("Обычные порты: IMAP — 993 (SSL) или 143 (STARTTLS), POP3 — 995 (SSL) или 110, "
              "SMTP — 465 (SSL) или 587 (STARTTLS).")


class StepFailed(Exception):
    def __init__(self, detail: str, hint: str = ""):
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


class StepWarning(Exception):
    """Шаг прошёл, но с оговоркой — проверка идёт дальше."""

    def __init__(self, detail: str, hint: str = ""):
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


# --- общие шаги ----------------------------------------------------------------

def _dns(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise StepFailed(f"адрес {host} не найден ({exc.strerror or exc})",
                         "Проверьте имя сервера в настройках почты.") from exc
    addrs = list(dict.fromkeys(info[4][0] for info in infos))
    more = f" и ещё {len(addrs) - 3}" if len(addrs) > 3 else ""
    return ", ".join(addrs[:3]) + more


def _tcp(host: str, port: int, outgoing_smtp: bool) -> str:
    started = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except TimeoutError as exc:
        if outgoing_smtp:
            hint = ("Если с другого компьютера этот сервер и порт доступны — исходящие подключения "
                    "закрыты на стороне сервера сайта: многие хостинги VPS блокируют SMTP (25, 465, 587) "
                    "против спама. Откройте их через поддержку хостинга или попробуйте другой порт SMTP.")
        else:
            hint = ("Проверьте адрес и порт. Если они верны — подключение блокирует файрвол "
                    "или провайдер сервера сайта.")
        raise StepFailed(f"порт {port} не ответил за {CONNECT_TIMEOUT} с", hint) from exc
    except ConnectionRefusedError as exc:
        raise StepFailed(f"сервер отказал в подключении к порту {port}",
                         f"Порт закрыт — проверьте номер порта. {PORTS_HINT}") from exc
    except OSError as exc:
        raise StepFailed(f"не удалось подключиться к порту {port}: {exc.strerror or exc}",
                         "Проверьте, что у сервера сайта есть доступ в интернет.") from exc
    sock.close()
    return f"порт {port} открыт, {round((time.monotonic() - started) * 1000)} мс"


def _tls(host: str, port: int) -> str:
    """Рукопожатие с проверкой сертификата. imaplib/poplib/smtplib в режиме
    SSL сертификат не проверяют, поэтому плохой сертификат — предупреждение,
    а не ошибка: вход всё равно пройдёт."""
    def handshake(context: ssl.SSLContext) -> tuple[str, dict]:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                return tls.version() or "TLS", tls.getpeercert() or {}

    try:
        version, cert = handshake(ssl.create_default_context())
    except ssl.SSLCertVerificationError as exc:
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        try:
            version, _cert = handshake(unverified)
        except (OSError, ssl.SSLError):
            version = "TLS"
        raise StepWarning(f"{version}, сертификат не прошёл проверку: {exc.verify_message or exc}",
                          "Соединение шифруется, но подмену сервера сертификат не исключает. "
                          "Проверьте имя сервера — оно должно совпадать с именем в сертификате.") from exc
    except ssl.SSLError as exc:
        raise StepFailed(f"рукопожатие TLS не удалось: {exc.reason or exc}",
                         "Похоже, на этом порту нет SSL: выберите в настройках STARTTLS или другой порт. "
                         + PORTS_HINT) from exc
    except TimeoutError as exc:
        raise StepFailed(f"сервер не ответил на рукопожатие TLS за {CONNECT_TIMEOUT} с",
                         "Похоже, на этом порту нет SSL. " + PORTS_HINT) from exc
    except OSError as exc:
        raise StepFailed(f"соединение оборвалось при рукопожатии TLS: {exc.strerror or exc}") from exc
    not_after = cert.get("notAfter")
    if not_after:
        expires = time.strftime("%d.%m.%Y", time.gmtime(ssl.cert_time_to_seconds(not_after)))
        return f"{version}, сертификат действителен до {expires}"
    return version


def _exc_text(exc: Exception) -> str:
    """Текст ошибки без b'…': imaplib/poplib кладут в args ответ сервера
    байтами, smtplib — (код, байты)."""
    if isinstance(exc, UnicodeError):
        return _connection_error_message(exc)
    smtp_error = getattr(exc, "smtp_error", None)
    if smtp_error is not None:
        return f"{getattr(exc, 'smtp_code', '')} {_short(smtp_error)}".strip()
    if exc.args and isinstance(exc.args[0], bytes):
        return _short(exc.args[0])
    return _short(str(exc)) or type(exc).__name__


def _protocol_error(exc: Exception, what: str) -> StepFailed:
    if isinstance(exc, ssl.SSLError):
        return StepFailed(f"{what}: ошибка TLS — {exc.reason or exc}",
                          "Проверьте тип шифрования в настройках. " + PORTS_HINT)
    if isinstance(exc, TimeoutError):
        return StepFailed(f"{what}: сервер не ответил за {CONNECT_TIMEOUT} с",
                          "Порт отвечает, но не по этому протоколу или шифрованию. " + PORTS_HINT)
    return StepFailed(f"{what}: {_exc_text(exc)}")


def _login_error(exc: Exception) -> StepFailed:
    if isinstance(exc, UnicodeError):
        return StepFailed(_connection_error_message(exc))
    return StepFailed(f"сервер отклонил вход: {_exc_text(exc)}",
                      "Проверьте логин (обычно это полный адрес почты) и пароль. Некоторые почтовые "
                      "службы требуют отдельный пароль приложения.")


def _short(text) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = " ".join(str(text or "").split())
    return text[:120] + ("…" if len(text) > 120 else "")


# --- протоколы ---------------------------------------------------------------------

class _ImapProbe:
    def __init__(self, settings: MailboxSettings):
        self.settings = settings
        self.conn: imaplib.IMAP4 | None = None

    def greeting(self) -> str:
        s = self.settings
        try:
            if s.incoming_encryption == MailEncryption.SSL:
                self.conn = imaplib.IMAP4_SSL(s.incoming_host, s.incoming_port, timeout=CONNECT_TIMEOUT)
            else:
                self.conn = imaplib.IMAP4(s.incoming_host, s.incoming_port, timeout=CONNECT_TIMEOUT)
                if s.incoming_encryption == MailEncryption.STARTTLS:
                    self.conn.starttls()
        except (OSError, imaplib.IMAP4.error, UnicodeError) as exc:
            raise _protocol_error(exc, "IMAP") from exc
        suffix = ", STARTTLS выполнен" if s.incoming_encryption == MailEncryption.STARTTLS else ""
        return _short(self.conn.welcome) + suffix

    def login(self) -> str:
        try:
            self.conn.login(self.settings.username or "", _decrypted_password(self.settings))
        except (OSError, imaplib.IMAP4.error, UnicodeError) as exc:
            raise _login_error(exc) from exc
        return f"вход как {self.settings.username}"

    def folders(self) -> str:
        s = self.settings
        configured = [("Входящие", "INBOX"), ("Отправленные", s.sent_folder), ("Черновики", s.drafts_folder),
                      ("Спам", s.spam_folder), ("Корзина", s.trash_folder)]
        found, missing = [], []
        for label, name in configured:
            if not name:
                continue
            try:
                typ, data = self.conn.select(f'"{name}"', readonly=True)  # EXAMINE — только чтение
                problem = None if typ == "OK" else _short(data[0] if data else typ)
            except (OSError, imaplib.IMAP4.error) as exc:
                problem = _short(exc)
            if problem is None:
                count = _short(data[0]) if data and data[0] is not None else "?"
                found.append(f"{label}: {count}")
            elif name == "INBOX":
                raise StepFailed(f"не открывается INBOX: {problem}")
            else:
                missing.append(f"{label} ({name}): {problem}")
        detail = "писем — " + ", ".join(found)
        if missing:
            raise StepWarning(detail + ". Не найдены: " + "; ".join(missing),
                              "Исправьте имена папок в настройках почты — точные имена видны в почтовой программе.")
        return detail

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass


class _Pop3Probe:
    def __init__(self, settings: MailboxSettings):
        self.settings = settings
        self.conn: poplib.POP3 | None = None

    def greeting(self) -> str:
        s = self.settings
        try:
            if s.incoming_encryption == MailEncryption.SSL:
                self.conn = poplib.POP3_SSL(s.incoming_host, s.incoming_port, timeout=CONNECT_TIMEOUT)
            else:
                self.conn = poplib.POP3(s.incoming_host, s.incoming_port, timeout=CONNECT_TIMEOUT)
                if s.incoming_encryption == MailEncryption.STARTTLS:
                    self.conn.stls()
        except (OSError, poplib.error_proto, UnicodeError) as exc:
            raise _protocol_error(exc, "POP3") from exc
        suffix = ", STLS выполнен" if s.incoming_encryption == MailEncryption.STARTTLS else ""
        return _short(self.conn.getwelcome()) + suffix

    def login(self) -> str:
        try:
            self.conn.user(self.settings.username or "")
            self.conn.pass_(_decrypted_password(self.settings))
            count, _size = self.conn.stat()
        except (OSError, poplib.error_proto, UnicodeError) as exc:
            raise _login_error(exc) from exc
        return f"вход как {self.settings.username}, писем в ящике: {count}"

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                pass


class _SmtpProbe:
    def __init__(self, settings: MailboxSettings):
        self.settings = settings
        self.conn: smtplib.SMTP | None = None

    def greeting(self) -> str:
        s = self.settings
        # Хост — в конструктор: SMTP_SSL берёт из него имя для TLS (SNI),
        # connect() без него падает. Приветствие 220 smtplib проверяет сам,
        # в подробностях — первая строка ответа на EHLO (имя сервера).
        try:
            if s.smtp_encryption == MailEncryption.SSL:
                self.conn = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=CONNECT_TIMEOUT)
            else:
                self.conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=CONNECT_TIMEOUT)
            self.conn.ehlo()
            if s.smtp_encryption == MailEncryption.STARTTLS:
                self.conn.starttls()
                self.conn.ehlo()
        except (OSError, smtplib.SMTPException, UnicodeError) as exc:
            raise _protocol_error(exc, "SMTP") from exc
        suffix = ", STARTTLS выполнен" if s.smtp_encryption == MailEncryption.STARTTLS else ""
        server = (self.conn.ehlo_resp or b"").split(b"\n")[0]
        return (_short(server) or "сервер ответил") + suffix

    def login(self) -> str:
        try:
            self.conn.login(self.settings.username or "", _decrypted_password(self.settings))
        except smtplib.SMTPNotSupportedError as exc:
            raise StepFailed("сервер не предлагает вход (AUTH) на этом подключении",
                             "Обычно вход доступен только после шифрования — выберите SSL или STARTTLS. "
                             + PORTS_HINT) from exc
        except (OSError, smtplib.SMTPException, UnicodeError) as exc:
            raise _login_error(exc) from exc
        return f"вход как {self.settings.username}"

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                pass


# --- сценарий ------------------------------------------------------------------------

Step = tuple[str, str, Callable[[], str]]


def _group_steps(host: str, port: int, encryption: MailEncryption, probe, protocol: str,
                 outgoing_smtp: bool) -> list[Step]:
    steps: list[Step] = [
        ("dns", "Адрес сервера (DNS)", lambda: _dns(host, port)),
        ("tcp", "Подключение к порту", lambda: _tcp(host, port, outgoing_smtp)),
    ]
    if encryption == MailEncryption.SSL:
        steps.append(("tls", "Шифрование (TLS) и сертификат", lambda: _tls(host, port)))
    steps.append(("greeting", f"Приветствие сервера ({protocol})", probe.greeting))
    steps.append(("login", "Вход (логин и пароль)", probe.login))
    if isinstance(probe, _ImapProbe):
        steps.append(("folders", "Папки", probe.folders))
    return steps


def _groups(settings: MailboxSettings):
    s = settings
    incoming_protocol = "POP3" if s.incoming_protocol == MailProtocol.POP3 else "IMAP"
    incoming_probe = _Pop3Probe(s) if incoming_protocol == "POP3" else _ImapProbe(s)
    yield ("incoming", f"Входящая почта — {incoming_protocol} {s.incoming_host}:{s.incoming_port}",
           incoming_protocol, incoming_probe,
           _group_steps(s.incoming_host, s.incoming_port, s.incoming_encryption, incoming_probe,
                        incoming_protocol, outgoing_smtp=False))
    smtp_probe = _SmtpProbe(s)
    yield ("smtp", f"Исходящая почта — SMTP {s.smtp_host}:{s.smtp_port}", "SMTP", smtp_probe,
           _group_steps(s.smtp_host, s.smtp_port, s.smtp_encryption, smtp_probe, "SMTP", outgoing_smtp=True))


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def run(settings: MailboxSettings) -> Iterator[dict]:
    """События шагов, затем {"type": "done", "ok": …, "errors": [...]}.
    errors — короткие строки вида «SMTP: подключение к порту: …» для
    settings.last_error."""
    errors: list[str] = []
    for group, group_title, protocol, probe, steps in _groups(settings):
        base = {"type": "step", "group": group, "group_title": group_title}
        failed = False
        try:
            for step_id, title, action in steps:
                event = {**base, "id": step_id, "title": title}
                if failed:
                    yield {**event, "status": SKIP, "detail": "", "hint": ""}
                    continue
                yield {**event, "status": RUNNING, "detail": "", "hint": ""}
                try:
                    detail = action()
                except StepWarning as warn:
                    yield {**event, "status": WARN, "detail": warn.detail, "hint": warn.hint}
                except StepFailed as fail:
                    failed = True
                    errors.append(f"{protocol}: {_lower_first(title)}: {fail.detail}")
                    yield {**event, "status": FAIL, "detail": fail.detail, "hint": fail.hint}
                except Exception as exc:  # непредвиденное — тоже видно в окне, а не 500
                    failed = True
                    errors.append(f"{protocol}: {_lower_first(title)}: {_short(exc)}")
                    yield {**event, "status": FAIL, "detail": f"{type(exc).__name__}: {_short(exc)}", "hint": ""}
                else:
                    yield {**event, "status": OK, "detail": detail, "hint": ""}
        finally:
            probe.close()
    yield {"type": "done", "ok": not errors, "errors": errors}
