"""
Протокольная логика почты правления — IMAP/POP3 (чтение) и SMTP (отправка).
Ничего не знает про Flask/шаблоны — принимает уже расшифрованные параметры
через MailboxSettings, отдаёт простые датаклассы. См. app/mailbox.py
(роуты) и app/mail_html.py (санитайзинг HTML-тела для показа).

Письма нигде не кэшируются — каждый вызов открывает соединение, делает
запрос, закрывает соединение (см. IncomingMailClient.__enter__/__exit__).
Для маленького ящика правления это проще и надёжнее синхронизации в свою
таблицу (нет конфликтов чтения-после-записи, нет второй копии почты).

IMAP и POP3 объединены общим интерфейсом IncomingMailClient, но
принципиально разные по возможностям: IMAP поддерживает папки/флаги
(прочитано), POP3 — нет (плоский единственный ящик, без флагов на
сервере) — см. supports_folders/supports_flags, UI (mailbox/inbox.html)
их учитывает (бейдж "непрочитано" только для IMAP).

Три отдельные функции подключения (_connect_imap/_connect_pop3/_connect_smtp)
специально не объединены в одну "фабрику" — так их проще всего подменять в
тестах (monkeypatch одной функции), при этом вся бизнес-логика разбора
писем/пагинации остаётся настоящей и реально тестируется.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt
import email
import email.policy
import email.utils
import imaplib
import poplib
import re
import smtplib
from email.message import EmailMessage

from .bank_api import crypto
from .models import MailboxSettings, MailEncryption, MailProtocol

CONNECT_TIMEOUT = 15  # секунд — иначе зависший сервер повесит HTTP-воркер на неопределённое время
DEFAULT_FOLDER = "INBOX"


class MailError(Exception):
    """Любая ошибка связи с почтовым сервером (соединение/логин/протокол).
    Роуты ловят именно этот тип и кладут str(e) в MailboxSettings.last_error."""


@dataclasses.dataclass
class MessageSummary:
    uid: str                    # IMAP UID или номер сообщения POP3 — оба как str
    subject: str
    from_name: str | None
    from_addr: str | None
    to_addr: str | None         # первый адресат (для отображения в папке "Отправленные" — там свой from почти всегда одинаков и малополезен в списке)
    date: dt.datetime | None
    seen: bool | None           # None у POP3 — там нет флагов вовсе


@dataclasses.dataclass
class MessagePage:
    messages: list[MessageSummary]
    total: int
    page: int
    page_size: int

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


@dataclasses.dataclass
class AttachmentPart:
    index: int                       # порядковый номер среди вложений письма — стабилен для одного и того же сообщения
    filename: str
    content_type: str
    size: int
    content_id: str | None = None    # заполнено для inline-частей (multipart/related, referenced как cid:...)


@dataclasses.dataclass
class MessageDetail:
    uid: str
    subject: str
    from_name: str | None
    from_addr: str | None
    to_addrs: list[str]
    date: dt.datetime | None
    body_text: str | None
    body_html: str | None
    attachments: list[AttachmentPart]
    inline_images: dict[str, tuple[AttachmentPart, bytes]]  # content_id (без <>) -> (метаданные, байты)


# ---------------------------------------------------------------------------
# Разбор письма — общий для IMAP и POP3: обе реализации в итоге получают
# сырые байты целого письма и парсят их одной и той же функцией.
# ---------------------------------------------------------------------------

def decode_idn_address(addr: str | None) -> str:
    """Отображение адреса с раскодированным IDN-доменом: почтовые сервера
    отдают заголовки From/To в ASCII (punycode) для кириллических/юникодных
    доменов — "pravlenie@xn----dtbbg1boax0b.xn--p1ai" вместо
    "pravlenie@гм-восход.рф". Для показа человеку декодируем через
    стандартный кодек "idna"; сам адрес в модели/форме (то, что реально
    уходит на SMTP) не трогаем — используется только там, где строка идёт
    в текст, а не в атрибут формы (см. mailbox.py: _reply_prefill — "to" не
    декодируется, это реальный адрес получателя, а не подпись).
    Молча возвращает исходную строку, если декодировать не удалось (не
    punycode-домен, кривой ввод и т.п.) — не должно ронять показ письма."""
    if not addr or "@" not in addr:
        return addr or ""
    local, _, domain = addr.rpartition("@")
    try:
        decoded_domain = domain.encode("ascii").decode("idna")
    except (UnicodeError, LookupError):
        return addr
    return f"{local}@{decoded_domain}"


def _address_from_header(msg: email.message.Message, header: str) -> tuple[str | None, str | None]:
    raw = msg[header]
    if not raw:
        return None, None
    addresses = email.utils.getaddresses([str(raw)])
    if not addresses:
        return None, None
    name, addr = addresses[0]
    return (name or None), (addr or None)


def _addr_list_from_header(msg: email.message.Message, header: str) -> list[str]:
    raw = msg[header]
    if not raw:
        return []
    return [addr for _name, addr in email.utils.getaddresses([str(raw)]) if addr]


def _parse_date(msg: email.message.Message) -> dt.datetime | None:
    raw = msg["date"]
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None


def _get_content(part: email.message.Message) -> str:
    """get_content() падает на некорректно объявленной/отсутствующей
    кодировке — тогда декодируем сырой payload вручную с заменой
    нечитаемых байт, лишь бы не ронять весь просмотр письма."""
    try:
        return part.get_content()
    except (UnicodeDecodeError, LookupError, KeyError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _body_parts(msg: email.message.Message) -> tuple[object | None, object | None]:
    """(html_part, plain_part) — оба через get_body(), а не только "основной"
    body_part: у HTML-письма с multipart/alternative это два РАЗНЫХ объекта
    (html — то, что показываем; plain — текстовый fallback), и оба должны
    быть исключены из списка вложений ниже."""
    return msg.get_body(preferencelist=("html",)), msg.get_body(preferencelist=("plain",))


def _walk_non_body_parts(msg: email.message.Message):
    """Все конечные (не multipart) части письма, КРОМЕ самого тела
    (html/plain-альтернативы) — то есть вложения и inline-картинки, где бы
    они ни были вложены в дереве MIME.

    Важно: msg.iter_attachments() тут НЕ подходит — она не рекурсирует
    внутрь multipart/related, вложенного в multipart/alternative (типичная
    структура HTML-писем с inline-картинками: mixed( alternative(plain,
    related(html, image)), attachment )) — inline-картинка в такой
    структуре была бы молча потеряна. msg.walk() обходит дерево целиком,
    поэтому используется он, с явным исключением частей тела по identity."""
    html_part, plain_part = _body_parts(msg)
    body_ids = {id(p) for p in (html_part, plain_part) if p is not None}
    for part in msg.walk():
        if part.is_multipart() or id(part) in body_ids:
            continue
        yield part


def _extract_attachment(raw: bytes, index: int) -> tuple[AttachmentPart, bytes] | None:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    i = 0
    for part in _walk_non_body_parts(msg):
        i += 1
        if i != index:
            continue
        filename = part.get_filename() or f"attachment-{i}"
        payload = part.get_payload(decode=True) or b""
        return AttachmentPart(
            index=i, filename=filename, content_type=part.get_content_type(), size=len(payload),
        ), payload
    return None


def _parse_message(uid: str, raw: bytes) -> MessageDetail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    from_name, from_addr = _address_from_header(msg, "from")
    to_addrs = _addr_list_from_header(msg, "to")
    date = _parse_date(msg)

    html_part, plain_part = _body_parts(msg)
    body_html = _get_content(html_part) if html_part is not None else None
    body_text = _get_content(plain_part) if plain_part is not None else None
    if body_html is None and body_text is None:
        # Ни html, ни text/plain по преференции не нашлось (нестандартное
        # письмо) — берём что найдёт универсальный body_part как есть.
        body_part = msg.get_body(preferencelist=("html", "plain"))
        if body_part is not None:
            content = _get_content(body_part)
            if body_part.get_content_type() == "text/html":
                body_html = content
            else:
                body_text = content

    attachments: list[AttachmentPart] = []
    inline_images: dict[str, tuple[AttachmentPart, bytes]] = {}
    index = 0
    for part in _walk_non_body_parts(msg):
        index += 1
        filename = part.get_filename() or f"attachment-{index}"
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        content_id_raw = part.get("Content-ID")
        content_id = content_id_raw.strip("<>") if content_id_raw else None
        disposition = part.get_content_disposition()  # "inline" | "attachment" | None
        att = AttachmentPart(index=index, filename=filename, content_type=content_type, size=len(payload), content_id=content_id)
        if content_id and content_type.startswith("image/") and disposition != "attachment":
            inline_images[content_id] = (att, payload)
            continue  # inline-картинки подставляются в тело письма (см. mail_html.py), в списке вложений не дублируются
        attachments.append(att)

    subject = str(msg.get("subject", "")).strip() or "(без темы)"

    return MessageDetail(
        uid=uid, subject=subject, from_name=from_name, from_addr=from_addr,
        to_addrs=to_addrs, date=date, body_text=body_text, body_html=body_html,
        attachments=attachments, inline_images=inline_images,
    )


# ---------------------------------------------------------------------------
# Подключение — единственные точки, где вызываются классы стандартной
# библиотеки (imaplib/poplib/smtplib) — подменяются в тестах.
# ---------------------------------------------------------------------------

def _decrypted_password(settings: MailboxSettings) -> str:
    return crypto.decrypt(settings.password_encrypted) or ""


def _connection_error_message(exc: Exception) -> str:
    """UnicodeError (в т.ч. UnicodeEncodeError — ПОДКЛАСС ValueError) — если
    в логине/пароле есть нелатинские символы (например кириллический домен
    в адресе почты). imaplib/poplib/smtplib кодируют команды протокола в
    ASCII и падают с сырым UnicodeEncodeError — без этого except он бы
    проскочил мимо MailError и попал в общий обработчик форм
    (app/errors.py: _bad_form_input ловит ValueError НА УРОВНЕ ПРИЛОЖЕНИЯ),
    показывая пользователю бесполезное "проверьте правильность заполнения
    формы" вместо объяснения, что именно не так."""
    if isinstance(exc, UnicodeError):
        return (
            "логин или пароль содержат символы, которые нельзя передать по протоколу — "
            "используйте латиницу (для кириллического домена почты — его punycode-вариант, xn--...)"
        )
    return str(exc)


def _connect_imap(settings: MailboxSettings) -> imaplib.IMAP4:
    if not settings.incoming_host:
        raise MailError("IMAP: сервер не настроен")
    try:
        if settings.incoming_encryption == MailEncryption.SSL:
            conn = imaplib.IMAP4_SSL(settings.incoming_host, settings.incoming_port, timeout=CONNECT_TIMEOUT)
        else:
            conn = imaplib.IMAP4(settings.incoming_host, settings.incoming_port, timeout=CONNECT_TIMEOUT)
            if settings.incoming_encryption == MailEncryption.STARTTLS:
                conn.starttls()
        conn.login(settings.username or "", _decrypted_password(settings))
        return conn
    except (OSError, imaplib.IMAP4.error, UnicodeError) as exc:
        raise MailError(f"IMAP: {_connection_error_message(exc)}") from exc


def _connect_pop3(settings: MailboxSettings) -> poplib.POP3:
    if not settings.incoming_host:
        raise MailError("POP3: сервер не настроен")
    try:
        if settings.incoming_encryption == MailEncryption.SSL:
            conn = poplib.POP3_SSL(settings.incoming_host, settings.incoming_port, timeout=CONNECT_TIMEOUT)
        else:
            conn = poplib.POP3(settings.incoming_host, settings.incoming_port, timeout=CONNECT_TIMEOUT)
            if settings.incoming_encryption == MailEncryption.STARTTLS:
                conn.stls()
        conn.user(settings.username or "")
        conn.pass_(_decrypted_password(settings))
        return conn
    except (OSError, poplib.error_proto, UnicodeError) as exc:
        raise MailError(f"POP3: {_connection_error_message(exc)}") from exc


def _connect_smtp(settings: MailboxSettings) -> smtplib.SMTP:
    if not settings.smtp_host:
        raise MailError("SMTP: сервер не настроен")
    try:
        if settings.smtp_encryption == MailEncryption.SSL:
            conn = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=CONNECT_TIMEOUT)
        else:
            conn = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=CONNECT_TIMEOUT)
            if settings.smtp_encryption == MailEncryption.STARTTLS:
                conn.starttls()
        conn.login(settings.username or "", _decrypted_password(settings))
        return conn
    except (OSError, smtplib.SMTPException, UnicodeError) as exc:
        raise MailError(f"SMTP: {_connection_error_message(exc)}") from exc


# ---------------------------------------------------------------------------
# Единый интерфейс чтения
# ---------------------------------------------------------------------------

class IncomingMailClient(abc.ABC):
    supports_folders: bool = False
    supports_flags: bool = False

    def __enter__(self) -> "IncomingMailClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def list_messages(self, page: int, page_size: int = 25, folder: str = DEFAULT_FOLDER) -> MessagePage: ...

    @abc.abstractmethod
    def get_message(self, uid: str, folder: str = DEFAULT_FOLDER) -> MessageDetail: ...

    @abc.abstractmethod
    def get_attachment(self, uid: str, index: int, folder: str = DEFAULT_FOLDER) -> tuple[AttachmentPart, bytes]: ...

    @abc.abstractmethod
    def delete_message(self, uid: str, folder: str = DEFAULT_FOLDER) -> None: ...


_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


class ImapMailClient(IncomingMailClient):
    supports_folders = True
    supports_flags = True

    def __init__(self, settings: MailboxSettings):
        self.settings = settings
        self.conn = _connect_imap(settings)
        self._selected_folder: str | None = None
        self._ensure_selected(DEFAULT_FOLDER)  # заодно проверка, что подключение реально работает, не только login

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.conn.logout()
        except Exception:
            pass

    def _ensure_selected(self, folder: str) -> None:
        """SELECT нужен один раз на папку за время жизни соединения — эта
        функция не даёт слать лишний SELECT, если предыдущая операция уже
        открыла ту же папку (список/письмо/вложение/удаление на одной
        странице обычно работают с одной и той же папкой)."""
        if self._selected_folder == folder:
            return
        try:
            typ, _data = self.conn.select(f'"{folder}"')
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP: не удалось открыть папку «{folder}»: {exc}") from exc
        if typ != "OK":
            raise MailError(f"IMAP: не удалось открыть папку «{folder}» — возможно, она не существует")
        self._selected_folder = folder

    def list_messages(self, page: int, page_size: int = 25, folder: str = DEFAULT_FOLDER) -> MessagePage:
        self._ensure_selected(folder)
        try:
            typ, data = self.conn.uid("search", None, "ALL")
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP SEARCH: {exc}") from exc
        if typ != "OK":
            raise MailError("IMAP: не удалось получить список писем")

        uid_bytes = data[0].split() if data and data[0] else []
        uid_bytes = list(reversed(uid_bytes))  # новые первыми (UID растут по мере поступления)
        total = len(uid_bytes)
        start = (page - 1) * page_size
        page_uids = uid_bytes[start:start + page_size]

        messages: list[MessageSummary] = []
        for uid in page_uids:
            try:
                typ, fdata = self.conn.uid("fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
            except imaplib.IMAP4.error as exc:
                raise MailError(f"IMAP FETCH: {exc}") from exc
            if typ != "OK" or not fdata or not isinstance(fdata[0], tuple):
                continue
            meta_line, header_bytes = fdata[0]
            flags_match = _FLAGS_RE.search(meta_line)
            seen = bool(flags_match) and b"\\Seen" in flags_match.group(1).split()
            msg = email.message_from_bytes(header_bytes, policy=email.policy.default)
            from_name, from_addr = _address_from_header(msg, "from")
            to_addrs = _addr_list_from_header(msg, "to")
            messages.append(MessageSummary(
                uid=uid.decode(), subject=str(msg.get("subject", "")).strip() or "(без темы)",
                from_name=from_name, from_addr=from_addr, to_addr=(to_addrs[0] if to_addrs else None),
                date=_parse_date(msg), seen=seen,
            ))
        return MessagePage(messages=messages, total=total, page=page, page_size=page_size)

    def _fetch_raw(self, uid: str, folder: str) -> bytes:
        self._ensure_selected(folder)
        try:
            typ, data = self.conn.uid("fetch", uid.encode(), "(RFC822)")
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP FETCH: {exc}") from exc
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise MailError("Письмо не найдено — возможно, было удалено на сервере")
        return data[0][1]

    def get_message(self, uid: str, folder: str = DEFAULT_FOLDER) -> MessageDetail:
        return _parse_message(uid, self._fetch_raw(uid, folder))

    def get_attachment(self, uid: str, index: int, folder: str = DEFAULT_FOLDER) -> tuple[AttachmentPart, bytes]:
        result = _extract_attachment(self._fetch_raw(uid, folder), index)
        if result is None:
            raise MailError("Вложение не найдено")
        return result

    def delete_message(self, uid: str, folder: str = DEFAULT_FOLDER) -> None:
        self._ensure_selected(folder)
        try:
            typ, _data = self.conn.uid("store", uid.encode(), "+FLAGS", "(\\Deleted)")
            if typ != "OK":
                raise MailError("IMAP: не удалось пометить письмо к удалению")
            self.conn.expunge()
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP: {exc}") from exc


class Pop3MailClient(IncomingMailClient):
    supports_folders = False
    supports_flags = False

    def __init__(self, settings: MailboxSettings):
        self.settings = settings
        self.conn = _connect_pop3(settings)
        self._supports_top = True

    def close(self) -> None:
        # DELE (см. delete_message) помечает письмо к удалению, но реально
        # удаляет только успешный QUIT в этой же сессии (RFC 1939) — именно
        # поэтому close() здесь ровно один раз за время жизни клиента.
        try:
            self.conn.quit()
        except Exception:
            pass

    def _check_folder(self, folder: str) -> None:
        if folder != DEFAULT_FOLDER:
            raise MailError("POP3: папки не поддерживаются протоколом — доступен только единственный ящик")

    def _fetch_raw(self, num: int) -> bytes:
        try:
            _resp, lines, _octets = self.conn.retr(num)
        except poplib.error_proto as exc:
            raise MailError(f"POP3 RETR: {exc}") from exc
        return b"\r\n".join(lines)

    def list_messages(self, page: int, page_size: int = 25, folder: str = DEFAULT_FOLDER) -> MessagePage:
        self._check_folder(folder)
        try:
            count, _size = self.conn.stat()
        except poplib.error_proto as exc:
            raise MailError(f"POP3 STAT: {exc}") from exc

        total = count
        numbers = list(range(count, 0, -1))  # новые первыми (обычно совпадает с порядком поступления)
        start = (page - 1) * page_size
        page_numbers = numbers[start:start + page_size]

        messages: list[MessageSummary] = []
        for num in page_numbers:
            header_bytes = None
            if self._supports_top:
                try:
                    _resp, lines, _octets = self.conn.top(num, 0)
                    header_bytes = b"\r\n".join(lines)
                except poplib.error_proto:
                    self._supports_top = False  # сервер не поддерживает TOP — дальше сразу RETR
            if header_bytes is None:
                header_bytes = self._fetch_raw(num)
            msg = email.message_from_bytes(header_bytes, policy=email.policy.default)
            from_name, from_addr = _address_from_header(msg, "from")
            to_addrs = _addr_list_from_header(msg, "to")
            messages.append(MessageSummary(
                uid=str(num), subject=str(msg.get("subject", "")).strip() or "(без темы)",
                from_name=from_name, from_addr=from_addr, to_addr=(to_addrs[0] if to_addrs else None),
                date=_parse_date(msg), seen=None,
            ))
        return MessagePage(messages=messages, total=total, page=page, page_size=page_size)

    def get_message(self, uid: str, folder: str = DEFAULT_FOLDER) -> MessageDetail:
        self._check_folder(folder)
        return _parse_message(uid, self._fetch_raw(int(uid)))

    def get_attachment(self, uid: str, index: int, folder: str = DEFAULT_FOLDER) -> tuple[AttachmentPart, bytes]:
        self._check_folder(folder)
        result = _extract_attachment(self._fetch_raw(int(uid)), index)
        if result is None:
            raise MailError("Вложение не найдено")
        return result

    def delete_message(self, uid: str, folder: str = DEFAULT_FOLDER) -> None:
        self._check_folder(folder)
        try:
            self.conn.dele(int(uid))
        except poplib.error_proto as exc:
            raise MailError(f"POP3 DELE: {exc}") from exc


def get_incoming_client(settings: MailboxSettings) -> IncomingMailClient:
    if settings.incoming_protocol == MailProtocol.POP3:
        return Pop3MailClient(settings)
    return ImapMailClient(settings)


def test_incoming_connection(settings: MailboxSettings) -> None:
    """Логин + выход. Поднимает MailError при неудаче."""
    with get_incoming_client(settings):
        pass


def test_smtp_connection(settings: MailboxSettings) -> None:
    conn = _connect_smtp(settings)
    try:
        conn.quit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def _save_to_sent_folder(settings: MailboxSettings, raw: bytes) -> None:
    """SMTP сам не сохраняет копию отправленного письма никуда (это не его
    задача — сохранением в "Отправленные" в обычном почтовом клиенте
    занимается IMAP APPEND отдельным запросом после успешной отправки).
    Только для IMAP (POP3 не пишущий протокол, папок у него нет вовсе) и
    только если председатель указал имя папки в настройках.

    Best-effort: ошибка здесь НЕ должна ронять всю отправку — письмо уже
    реально ушло получателю к этому моменту, отсутствие копии в
    "Отправленные" — второстепенная проблема, не повод показывать
    пользователю "не удалось отправить письмо"."""
    if settings.incoming_protocol != MailProtocol.IMAP or not settings.sent_folder:
        return
    try:
        conn = _connect_imap(settings)
    except MailError:
        return
    try:
        folder = settings.sent_folder
        typ, _data = conn.append(f'"{folder}"', "(\\Seen)", None, raw)
        if typ != "OK":
            # Папки ещё нет — создаём и пробуем ещё раз один раз.
            conn.create(f'"{folder}"')
            conn.append(f'"{folder}"', "(\\Seen)", None, raw)
    except Exception:
        pass
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def send_message(
    settings: MailboxSettings,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> None:
    """attachments — список (filename, content_type, data). Поднимает MailError.

    body_html — отрендеренная разметка тулбара формы письма (см. mailbox.py:
    compose, app/news_format.py: render_html, тот же приём, что и у
    новостей/вики). set_content()+add_alternative() после него — стандартный
    способ EmailMessage собрать multipart/alternative (текст — fallback для
    клиентов без HTML, html — то, что покажет обычный почтовый клиент)."""
    msg = EmailMessage()
    from_addr = settings.username or ""
    msg["From"] = email.utils.formataddr((settings.from_name or "", from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    for filename, content_type, data in (attachments or []):
        maintype, _sep, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)

    conn = _connect_smtp(settings)
    try:
        conn.send_message(msg)
    except (smtplib.SMTPException, UnicodeError) as exc:
        raise MailError(f"SMTP: {_connection_error_message(exc)}") from exc
    finally:
        try:
            conn.quit()
        except Exception:
            pass

    _save_to_sent_folder(settings, msg.as_bytes())
