#!/usr/bin/env python3
"""
Архив почты правления: письма с почтового сервера копируются в
INSTANCE_DIR/mail_backup/ файлами .eml. Запускается по cron
(scripts/backup_mailbox.sh) перед backup_offsite, который отправляет
каталог во внешний бэкап; агент на компьютере председателя тоже забирает
его к себе. См. docs/deployment.md, «Внешний бэкап».

Само приложение письма не хранит (app/mail_client.py), они живут только на
почтовом сервере. Если сервер пропадёт, без этого архива пропадёт и почта.
Подключение — из тех же настроек «Почты правления» (MailboxSettings),
отдельный конфиг не нужен.

Работает только на добавление: письмо, которого уже нет на сервере
(удалили, переложили), в архиве остаётся. Повторный запуск скачивает
только новые.

  IMAP: все папки (LIST), только на чтение (EXAMINE + BODY.PEEK, флаг
        «прочитано» не меняется), раскладка
        mail_backup/imap/<папка>/<UIDVALIDITY>/<UID>.eml. Имя папки — как
        на сервере (modified UTF-7, ASCII), «/» и прочее заменены на «_».
        Смена UIDVALIDITY (сервер перенумеровал письма) даёт новый
        подкаталог, а не перезапись.
  POP3: mail_backup/pop3/<UIDL>.eml; без расширения UIDL — не умеем
        отличить новое письмо от старого, бэкап пропускается с ошибкой.

Запуск вручную:
    cd /path/to/project && python3 scripts/backup_mailbox.py
"""
import datetime as dt
import hashlib
import imaplib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FETCH_BATCH = 50

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
_UID_RE = re.compile(rb"UID (\d+)")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._&+,=-]")


def log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def safe_name(name: str) -> str:
    """Имя для файловой системы. Слишком длинное или ставшее пустым/«.»
    после замены — дополняется хешем исходного, чтобы не склеивались
    разные папки/UIDL."""
    cleaned = _SAFE_NAME_RE.sub("_", name).strip(".") or "_"
    if cleaned != name or len(cleaned) > 100:
        digest = hashlib.sha1(name.encode("utf-8", "surrogateescape")).hexdigest()[:8]
        cleaned = f"{cleaned[:90]}~{digest}"
    return cleaned


def _write_atomic(path: str, data: bytes) -> None:
    with open(path + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(path + ".tmp", path)


def parse_list_line(line: bytes) -> str | None:
    """Ответ LIST → имя папки (как на сервере) или None для \\Noselect."""
    match = _LIST_RE.match(line)
    if not match:
        return None
    if b"\\noselect" in match.group("flags").lower() or b"\\nonexistent" in match.group("flags").lower():
        return None
    name = match.group("name").strip()
    if name.startswith(b'"') and name.endswith(b'"'):
        name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return name.decode("ascii", "surrogateescape")


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def backup_imap_folder(conn: imaplib.IMAP4, folder: str, root: str) -> int:
    typ, _ = conn.select(_quote(folder), readonly=True)
    if typ != "OK":
        log(f"IMAP: папку {folder!r} не открыть — пропущена")
        return 0
    typ, data = conn.response("UIDVALIDITY")
    uidvalidity = (data[0].decode() if data and data[0] else "0")
    target = os.path.join(root, safe_name(folder), safe_name(uidvalidity))
    os.makedirs(target, exist_ok=True)

    typ, data = conn.uid("SEARCH", None, "ALL")
    if typ != "OK":
        raise RuntimeError(f"IMAP: UID SEARCH в {folder!r}: {data!r}")
    uids = [uid.decode() for uid in (data[0] or b"").split()]
    have = {name[:-4] for name in os.listdir(target) if name.endswith(".eml")}
    missing = [uid for uid in uids if uid not in have]

    saved = 0
    for start in range(0, len(missing), FETCH_BATCH):
        batch = missing[start:start + FETCH_BATCH]
        typ, data = conn.uid("FETCH", ",".join(batch), "(UID BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(f"IMAP: UID FETCH в {folder!r}: {data!r}")
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            match = _UID_RE.search(item[0])
            if not match:
                continue
            _write_atomic(os.path.join(target, f"{match.group(1).decode()}.eml"), item[1])
            saved += 1
    return saved


def backup_imap(settings, root: str) -> str:
    from app.mail_client import _connect_imap

    conn = _connect_imap(settings)
    try:
        typ, data = conn.list()
        if typ != "OK":
            raise RuntimeError(f"IMAP: LIST: {data!r}")
        folders = [name for name in (parse_list_line(line) for line in data if isinstance(line, bytes)) if name]
        imap_root = os.path.join(root, "imap")
        total = 0
        for folder in folders:
            saved = backup_imap_folder(conn, folder, imap_root)
            if saved:
                log(f"IMAP: {folder}: новых писем {saved}")
            total += saved
        return f"IMAP: папок {len(folders)}, новых писем {total}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def backup_pop3(settings, root: str) -> str:
    import poplib
    from app.mail_client import _connect_pop3

    conn = _connect_pop3(settings)
    try:
        try:
            _resp, lines, _octets = conn.uidl()
        except poplib.error_proto as exc:
            raise RuntimeError(f"POP3: сервер не поддерживает UIDL — архив невозможен ({exc})") from exc
        target = os.path.join(root, "pop3")
        os.makedirs(target, exist_ok=True)
        have = {name[:-4] for name in os.listdir(target) if name.endswith(".eml")}
        saved = 0
        for line in lines:
            number, uidl = line.decode("ascii", "surrogateescape").split(" ", 1)
            name = safe_name(uidl)
            if name in have:
                continue
            _resp, body_lines, _octets = conn.retr(int(number))
            _write_atomic(os.path.join(target, f"{name}.eml"), b"\r\n".join(body_lines) + b"\r\n")
            saved += 1
        return f"POP3: писем на сервере {len(lines)}, новых {saved}"
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def main() -> int:
    from app import create_app, database
    from app.mail_client import MailError
    from config import INSTANCE_DIR
    from app.models import MailboxSettings, MailProtocol

    app = create_app()
    with app.app_context():
        settings = database.db_session.query(MailboxSettings).first()
        if settings is None or not settings.incoming_host:
            log("почта правления не настроена — архивировать нечего")
            return 0
        root = os.path.join(INSTANCE_DIR, "mail_backup")
        os.makedirs(root, exist_ok=True)
        os.chmod(root, 0o700)
        try:
            if settings.incoming_protocol == MailProtocol.POP3:
                message = backup_pop3(settings, root)
            else:
                message = backup_imap(settings, root)
        except (MailError, RuntimeError, OSError, imaplib.IMAP4.error) as exc:
            log(f"ОШИБКА: {exc}")
            return 1
        log(message)
        return 0


if __name__ == "__main__":
    sys.exit(main())
