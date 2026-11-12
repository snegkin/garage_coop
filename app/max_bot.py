"""
Бот мессенджера MAX — дублирование новостей сайта в канал/группу
(app/news.py, очередь app/messenger_outbox.py). Уведомлений людям через MAX
нет: только новости.

Bot API (dev.max.ru/docs-api, ноябрь 2026):
  - адрес https://platform-api2.max.ru, токен — заголовком Authorization
    (без «Bearer»); сертификат сервера выдан НУЦ Минцифры — корня нет в
    certifi, см. Config.MAX_API_CA_BUNDLE и _verify();
  - POST /messages?chat_id=… — {"text", "format": "html", "attachments",
    "notify"}; текст до 4000 символов, до 12 картинок в одном сообщении,
    файл — только отдельным сообщением (с картинками не сочетается);
  - вложение: POST /uploads?type=image|file -> {"url"}, файл туда полем
    multipart "data" -> токен; сразу после загрузки MAX его ещё
    обрабатывает (ошибка attachment.not.ready) — повторяем с паузой;
  - не больше 2 сообщений в секунду в один чат;
  - список чатов бота (GET /chats) API больше не отдаёт: ID канала
    узнаём из апдейта bot_added (GET /updates, scripts/poll_telegram.py ->
    poll_updates) и показываем на странице настроек (app/max_settings.py).
Бот должен быть администратором канала/группы.
"""
import datetime as dt
import json
import os
import tempfile
import time

import certifi
import requests
from flask import current_app, has_app_context
from markupsafe import escape

from . import database
from .api_log import mask_secrets_in_text
from .bank_api import crypto
from .messenger_errors import MessengerError, MessengerUncertain, is_connect_stage
from .models import MaxSettings, News

API_BASE = "https://platform-api2.max.ru"
REQUEST_TIMEOUT = 10
UPLOAD_TIMEOUT = 60

TEXT_LIMIT = 4000
# С запасом: как MAX считает длину HTML-текста, не документировано. Длиннее —
# обрезаем по абзацу, полная версия по ссылке.
NEWS_TEXT_LIMIT = 3500
NEWS_EXCERPT_LENGTH = 1500
IMAGES_PER_MESSAGE = 12
PHOTO_MAX_BYTES = 50 * 1024 * 1024
DOCUMENT_MAX_BYTES = 4 * 1024 * 1024 * 1024
# Паузы (секунды) перед повтором отправки, пока MAX обрабатывает только что
# загруженный файл (attachment.not.ready).
NOT_READY_DELAYS = (1, 2, 4, 8)
# Между сообщениями одной новости — лимит 2 сообщения в секунду на чат.
SEND_PAUSE = 0.6
_sleep = time.sleep  # подменяется в тестах


class MaxError(MessengerError):
    """Ошибка MAX (см. MessengerError). code — код ошибки из ответа API
    ("attachment.not.ready", "verify.token", …), если он был."""

    def __init__(self, message: str, status_code: int | None = None, transport: bool = False,
                 code: str | None = None):
        super().__init__(message, status_code=status_code, transport=transport)
        self.code = code


class MaxUncertain(MaxError, MessengerUncertain):
    """Запрос ушёл, но ответа нет — MAX мог его принять."""


def get_settings() -> MaxSettings | None:
    return database.db_session.query(MaxSettings).first()


def is_configured(settings: MaxSettings | None) -> bool:
    return bool(settings and settings.bot_token_encrypted)


def news_group_enabled(settings: MaxSettings | None) -> bool:
    return bool(is_configured(settings) and settings.news_chat_id)


def _token(settings: MaxSettings) -> str:
    token = crypto.decrypt(settings.bot_token_encrypted) if settings.bot_token_encrypted else None
    if not token:
        raise MaxError("Токен бота MAX не задан", status_code=0)
    return token


_bundle_cache: dict[str, str] = {}


def _verify():
    """Набор корней для проверки TLS: certifi + корень Минцифры
    (Config.MAX_API_CA_BUNDLE). requests принимает один файл, поэтому
    склеиваем их один раз во временный файл. Файла корня нет — certifi
    (тогда platform-api2.max.ru не откроется: SSLError в журнале очереди)."""
    path = current_app.config.get("MAX_API_CA_BUNDLE") if has_app_context() else None
    if not path or not os.path.isfile(path):
        return True
    if path not in _bundle_cache or not os.path.isfile(_bundle_cache[path]):
        with open(certifi.where(), "rb") as base, open(path, "rb") as extra:
            data = base.read() + b"\n" + extra.read()
        fd, combined = tempfile.mkstemp(prefix="max-ca-", suffix=".pem")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _bundle_cache[path] = combined
    return _bundle_cache[path]


def _request_error(exc: requests.RequestException, idempotent: bool) -> MaxError:
    message = f"MAX: {mask_secrets_in_text(str(exc))}"
    if is_connect_stage(exc) or idempotent:
        return MaxError(message, transport=True)
    return MaxUncertain(message, transport=True)


def _status_error(resp) -> MaxError:
    code = None
    detail = resp.text[:200]
    try:
        data = resp.json()
        code = data.get("code")
        detail = f"{code}: {data.get('message', '')}" if code else detail
    except ValueError:
        pass
    return MaxError(f"MAX: HTTP {resp.status_code}: {detail}", status_code=resp.status_code, code=code)


def _request(settings: MaxSettings, method: str, path: str, *, idempotent: bool,
             params: dict | None = None, payload: dict | None = None) -> dict:
    try:
        resp = requests.request(
            method, API_BASE + path, params=params, json=payload,
            headers={"Authorization": _token(settings)},
            timeout=REQUEST_TIMEOUT, verify=_verify(),
        )
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent) from exc
    if resp.status_code != 200:
        raise _status_error(resp)
    try:
        return resp.json()
    except ValueError:
        return {}


def get_me(settings: MaxSettings) -> dict:
    """Страница настроек — «Проверить подключение»."""
    return _request(settings, "GET", "/me", idempotent=True)


def get_chat(settings: MaxSettings, chat_id: int | str) -> dict:
    return _request(settings, "GET", f"/chats/{chat_id}", idempotent=True)


def upload(settings: MaxSettings, kind: str, filename: str, path: str) -> str:
    """Загружает файл (kind — "image" или "file") -> токен вложения.
    Повторная загрузка безвредна, поэтому любой сетевой сбой — повторяемый."""
    info = _request(settings, "POST", "/uploads", idempotent=True, params={"type": kind})
    url = info.get("url")
    if not url:
        raise MaxError(f"MAX: не выдан адрес загрузки ({info})")
    try:
        with open(path, "rb") as fh:
            resp = requests.post(url, files={"data": (filename, fh)},
                                 headers={"Authorization": _token(settings)},
                                 timeout=UPLOAD_TIMEOUT, verify=_verify())
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent=True) from exc
    except OSError as exc:
        raise MaxError(f"Файл вложения недоступен: {exc}", status_code=0) from exc
    if resp.status_code != 200:
        raise _status_error(resp)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    token = data.get("token") or info.get("token")
    if not token and isinstance(data.get("photos"), dict):
        # картинка: {"photos": {"<id>": {"token": "…"}}}
        token = next((p.get("token") for p in data["photos"].values() if isinstance(p, dict)), None)
    if not token:
        raise MaxError(f"MAX: не получен токен загруженного файла {filename}")
    return token


def send_message(settings: MaxSettings, chat_id: int | str, text: str | None,
                 attachments: list | None = None, html: bool = False, notify: bool = True,
                 disable_link_preview: bool = False) -> dict:
    """POST /messages. Пока MAX обрабатывает загруженные вложения
    (attachment.not.ready), ждём и повторяем — сообщение при этом ещё не
    создано, повтор не задвоит. Не дождались — повторяемая ошибка, очередь
    попробует позже (файл загрузится заново)."""
    body: dict = {"notify": notify}
    if text:
        body["text"] = text
    if html:
        body["format"] = "html"
    if attachments:
        body["attachments"] = attachments
    params = {"chat_id": chat_id}
    if disable_link_preview:
        params["disable_link_preview"] = "true"
    for delay in (*NOT_READY_DELAYS, None):
        try:
            return _request(settings, "POST", "/messages", idempotent=False, params=params, payload=body)
        except MaxError as exc:
            if exc.code != "attachment.not.ready":
                raise
            if delay is None:
                raise MaxError("MAX ещё обрабатывает вложения — повторим позже", code=exc.code) from exc
            _sleep(delay)


# ---------------------------------------------------------------------------
# Чаты бота (апдейты bot_added / bot_removed)
# ---------------------------------------------------------------------------

def known_chats(settings: MaxSettings | None) -> list[dict]:
    if not settings or not settings.known_chats:
        return []
    try:
        chats = json.loads(settings.known_chats)
    except ValueError:
        return []
    return chats if isinstance(chats, list) else []


def _remember_chat(settings: MaxSettings, chat_id, is_channel: bool) -> None:
    chats = [c for c in known_chats(settings) if str(c.get("chat_id")) != str(chat_id)]
    title = None
    try:
        title = get_chat(settings, chat_id).get("title")
    except MaxError:
        pass  # название — для удобства выбора, без него тоже можно
    chats.append({"chat_id": chat_id, "is_channel": bool(is_channel), "title": title,
                  "added_at": dt.datetime.utcnow().isoformat(timespec="seconds")})
    settings.known_chats = json.dumps(chats, ensure_ascii=False)


def _forget_chat(settings: MaxSettings, chat_id) -> None:
    chats = [c for c in known_chats(settings) if str(c.get("chat_id")) != str(chat_id)]
    settings.known_chats = json.dumps(chats, ensure_ascii=False)


def poll_updates() -> int:
    """GET /updates (без ожидания — cron раз в минуту): запоминает чаты и
    каналы, куда бота добавили (bot_added) или где он увидел сообщение, и
    забывает те, откуда убрали. -> число апдейтов. Бот не настроен — 0."""
    settings = get_settings()
    if not is_configured(settings):
        return 0
    params = {"timeout": 0, "limit": 100}
    if settings.last_marker is not None:
        params["marker"] = settings.last_marker
    try:
        data = _request(settings, "GET", "/updates", idempotent=True, params=params)
    except MaxError as exc:
        settings.last_error = str(exc)
        database.db_session.commit()
        raise
    updates = data.get("updates") or []
    seen = {str(c.get("chat_id")) for c in known_chats(settings)}
    for update in updates:
        kind = update.get("update_type")
        if kind == "bot_added" and update.get("chat_id") is not None:
            _remember_chat(settings, update["chat_id"], update.get("is_channel", False))
            seen.add(str(update["chat_id"]))
        elif kind == "bot_removed" and update.get("chat_id") is not None:
            _forget_chat(settings, update["chat_id"])
            seen.discard(str(update["chat_id"]))
        elif kind == "message_created":
            recipient = (update.get("message") or {}).get("recipient") or {}
            chat_id, chat_type = recipient.get("chat_id"), recipient.get("chat_type")
            if chat_id is not None and chat_type in ("chat", "channel") and str(chat_id) not in seen:
                _remember_chat(settings, chat_id, chat_type == "channel")
                seen.add(str(chat_id))
    if data.get("marker") is not None:
        settings.last_marker = data["marker"]
    settings.last_error = None
    settings.last_polled_at = dt.datetime.utcnow()
    database.db_session.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# Новости
# ---------------------------------------------------------------------------

def news_text(title: str, body: str, url: str) -> str:
    from .news_format import max_html, excerpt
    from .telegram_bot import _truncate_blocks, _visible_length

    rendered = max_html(body, base_url=url)
    if _visible_length(rendered) > NEWS_TEXT_LIMIT:
        rendered = _truncate_blocks(rendered, NEWS_TEXT_LIMIT) or str(escape(excerpt(body, NEWS_EXCERPT_LENGTH)[0]))
    return f"<b>{escape(title)}</b>\n\n{rendered}\n\n<a href=\"{escape(url)}\">Читать на сайте</a>"


def split_attachments(item: News, upload_folder: str):
    """-> (картинки, файлы, пропущенные). Картинки до 50 МБ идут в сообщение
    с текстом, остальное — файлами."""
    photos, documents, skipped = [], [], []
    for att in item.attachments:
        path = os.path.join(upload_folder, att.stored_filename)
        try:
            size = os.path.getsize(path)
        except OSError:
            skipped.append(att.original_filename)
            continue
        if att.is_image and size <= PHOTO_MAX_BYTES:
            photos.append((att, path))
        elif size <= DOCUMENT_MAX_BYTES:
            documents.append((att, path))
        else:
            skipped.append(att.original_filename)
    return photos, documents, skipped


def news_steps(settings: MaxSettings, item: News, url: str, upload_folder: str,
               album_first: bool = False, silent: bool = False) -> tuple[list, list[str]]:
    """Отправка новости в MaxSettings.news_chat_id по шагам (одно сообщение —
    один шаг, см. app/messenger_outbox.py): текст вместе с первыми 12
    картинками, остальные картинки — следующими сообщениями по 12, каждый
    файл — отдельным сообщением (MAX не сочетает файл с картинками).
    album_first не нужен: текст всегда в одном сообщении с картинками.
    silent — notify=false (в канале MAX, по документации, уведомляет всегда).
    -> (шаги, пропущенные файлы)."""
    chat_id = settings.news_chat_id
    text = news_text(item.title, item.body, url)
    photos, documents, skipped = split_attachments(item, upload_folder)
    chunks = [photos[i:i + IMAGES_PER_MESSAGE] for i in range(0, len(photos), IMAGES_PER_MESSAGE)] or [[]]

    def send(chunk, with_text: bool, pause: bool):
        if pause:
            _sleep(SEND_PAUSE)
        attachments = [{"type": "image", "payload": {"token": upload(settings, "image", att.original_filename, path)}}
                       for att, path in chunk]
        send_message(settings, chat_id, text if with_text else None, attachments=attachments,
                     html=with_text, notify=not silent, disable_link_preview=with_text)

    steps = [lambda n=n, chunk=chunk: send(chunk, with_text=(n == 0), pause=(n > 0))
             for n, chunk in enumerate(chunks)]

    def send_document(att, path):
        _sleep(SEND_PAUSE)
        token = upload(settings, "file", att.original_filename, path)
        send_message(settings, chat_id, None, attachments=[{"type": "file", "payload": {"token": token}}],
                     notify=not silent)

    for att, path in documents:
        steps.append(lambda att=att, path=path: send_document(att, path))
    return steps, skipped
