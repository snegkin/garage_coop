"""
Бот Telegram для уведомлений (app/notifications.py) — единственный
поддерживаемый способ реальной отправки, кроме email. Токен и имя бота
хранятся в единственной записи TelegramSettings (app/models.py), токен
зашифрован тем же Fernet, что и остальные секреты API (app/bank_api/crypto.py).

Bot API не позволяет написать произвольному @username — только тому, кто
уже сам открыл диалог с ботом (написал /start). Поэтому привязка аккаунта
устроена так:
  1. Человек нажимает «Привязать Telegram» в настройках профиля
     (app/cabinet.py: telegram_link_start) — генерируется одноразовый
     токен (Person.telegram_link_token), показывается диплинк
     https://t.me/<bot_username>?start=<token>.
  2. Человек открывает диплинк в Telegram и нажимает «Start» — Telegram
     отправляет боту апдейт с текстом "/start <token>".
  3. scripts/poll_messengers.py (cron, long polling через getUpdates) видит
     этот апдейт, находит Person по токену, сохраняет chat_id, сбрасывает
     токен (одноразовый) и отвечает приветственным сообщением.

Реальная отправка уведомлений (notifications.py: notify) использует уже
сохранённый chat_id — Person.telegram (свободный текст "вот мой аккаунт"
для отображения на карточке) в этом не участвует.
"""
import datetime as dt
import html
import json
import logging
import os
import re

import requests

from . import database
from .api_log import mask_secrets_in_text
from .bank_api import crypto
from .messenger_errors import MessengerError, MessengerUncertain, is_connect_stage
from .models import News, Person, TelegramSettings

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 10


class TelegramError(MessengerError):
    """Ошибка Telegram (см. MessengerError: status_code, transport)."""


class TelegramUncertain(TelegramError, MessengerUncertain):
    """Запрос ушёл, но ответа нет — Telegram мог его принять (см.
    MessengerUncertain)."""


def _request_error(exc: requests.RequestException, idempotent: bool) -> TelegramError:
    message = f"Telegram: {mask_secrets_in_text(str(exc))}"
    if is_connect_stage(exc) or idempotent:
        return TelegramError(message, transport=True)
    return TelegramUncertain(message, transport=True)


def _status_error(resp) -> TelegramError:
    return TelegramError(f"Telegram: HTTP {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)


def get_settings() -> TelegramSettings | None:
    return database.db_session.query(TelegramSettings).first()


def _token(settings: TelegramSettings) -> str:
    token = crypto.decrypt(settings.bot_token_encrypted)
    if not token:
        raise TelegramError("Токен бота не задан")
    return token


def _api(settings: TelegramSettings, token: str, method: str) -> str:
    base = (settings.api_base_url or API_BASE).rstrip("/")
    return f"{base}/bot{token}/{method}"


def _net(settings: TelegramSettings) -> dict:
    """Доп. аргументы requests: прокси, если задан (только для Telegram —
    передаётся в каждый запрос, а не через переменные окружения процесса,
    чтобы банк/СМС/eWeLink продолжали ходить напрямую)."""
    proxy = crypto.decrypt(settings.proxy_url_encrypted) if settings.proxy_url_encrypted else None
    return {"proxies": {"http": proxy, "https": proxy}} if proxy else {}


PROXY_SCHEMES = ("socks5h://", "socks5://", "http://", "https://")


def is_configured(settings: TelegramSettings | None) -> bool:
    return bool(settings and settings.bot_token_encrypted and settings.bot_username)


def send_message(settings: TelegramSettings, chat_id: int | str, text: str,
                 parse_mode: str | None = None, disable_preview: bool = False,
                 silent: bool = False) -> None:
    """Поднимает TelegramError при сбое — вызывающий код (notifications.notify)
    сам решает, что с этим делать (обычно — залогировать и не ронять
    остальную обработку). chat_id — число (личный чат, группа -100…) или
    "@имя" публичного канала/группы (см. TelegramSettings.news_chat_id)."""
    token = _token(settings)
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_preview:
        payload["link_preview_options"] = {"is_disabled": True}
    if silent:
        payload["disable_notification"] = True
    try:
        resp = requests.post(
            _api(settings, token, "sendMessage"),
            json=payload,
            timeout=REQUEST_TIMEOUT,
            **_net(settings),
        )
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent=False) from exc
    if resp.status_code != 200:
        raise _status_error(resp)
    try:
        return (resp.json().get("result") or {}).get("message_id")
    except ValueError:
        return None


def _call(settings: TelegramSettings, method: str, payload: dict) -> None:
    """Простой вызов Bot API (deleteMessage, pinChatMessage, …) — все
    идемпотентны: повтор после обрыва ничего не задвоит."""
    try:
        resp = requests.post(_api(settings, _token(settings), method), json=payload,
                             timeout=REQUEST_TIMEOUT, **_net(settings))
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent=True) from exc
    if resp.status_code != 200:
        raise _status_error(resp)


def _message_id(external_id: str) -> int:
    """id сообщения Telegram из external_id: у копий — просто число, у
    сообщений, пришедших из группы (BoardChatMessage.external_message_id), —
    «chat_id:message_id»."""
    return int(str(external_id).rsplit(":", 1)[-1])


def delete_group_message(settings: TelegramSettings, chat_id, external_id: str) -> None:
    _call(settings, "deleteMessage", {"chat_id": chat_id, "message_id": _message_id(external_id)})


def pin_group_message(settings: TelegramSettings, chat_id, external_id: str) -> None:
    _call(settings, "pinChatMessage", {"chat_id": chat_id, "message_id": _message_id(external_id),
                                       "disable_notification": True})


def unpin_group_message(settings: TelegramSettings, chat_id, external_id: str) -> None:
    _call(settings, "unpinChatMessage", {"chat_id": chat_id, "message_id": _message_id(external_id)})


def send_notification(settings: TelegramSettings, chat_id: int | str, text: str) -> None:
    """Уведомление человеку (очередь, kind="message") — тот же вызов, что у
    max_bot.send_notification."""
    send_message(settings, chat_id, text)


def send_group_message(settings: TelegramSettings, chat_id: int | str, text: str) -> str | None:
    """Сообщение в группу (мост чата правления, очередь kind="group").
    -> id отправленного сообщения (для удаления/закрепа потом)."""
    message_id = send_message(settings, chat_id, text)
    return str(message_id) if message_id is not None else None


def get_updates(settings: TelegramSettings, offset: int | None, timeout: int = 0) -> list[dict]:
    """timeout=0 — обычный short-poll (используется из cron раз в минуту,
    держать HTTP-соединение открытым долгим long-poll'ом внутри cron-скрипта
    смысла не имеет — следующий запуск всё равно через минуту)."""
    token = _token(settings)
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(_api(settings, token, "getUpdates"), params=params,
                            timeout=REQUEST_TIMEOUT + timeout, **_net(settings))
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent=True) from exc
    if resp.status_code != 200:
        raise _status_error(resp)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"Telegram: {data.get('description', 'unknown error')}")
    return data.get("result", [])


def get_me(settings: TelegramSettings) -> dict:
    """Используется только на странице настроек — «Проверить подключение»
    (показывает имя бота, подтверждает, что токен рабочий)."""
    token = _token(settings)
    try:
        resp = requests.get(_api(settings, token, "getMe"), timeout=REQUEST_TIMEOUT, **_net(settings))
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent=True) from exc
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"Telegram: {data.get('description', 'unknown error')}")
    return data["result"]


def _is_chatid_command(text: str) -> bool:
    """/chatid или /chatid@имя_бота (так Telegram подставляет команду,
    выбранную из меню в группе)."""
    # Без учёта регистра: «/chatId», набранное руками, — та же команда.
    command = text.split(maxsplit=1)[0].lower() if text else ""
    return command == "/chatid" or command.startswith("/chatid@")


def _reply_chat_id(settings: TelegramSettings, chat: dict) -> None:
    """Подсказка для настройки дублирования новостей в группу
    (TelegramSettings.news_chat_id): ID закрытой группы нигде в интерфейсе
    Telegram не виден, а бот, добавленный в группу, знает его из апдейта."""
    try:
        send_message(
            settings, chat["id"],
            f"ID этого чата: {chat['id']}\n"
            "Впишите его в настройках Telegram на сайте кооператива "
            "(поле «Группа для новостей» или «Группа правления»).",
        )
    except TelegramError as exc:
        # Раньше ошибка молча терялась — и /chatid «просто не отвечал».
        logger.warning("Telegram: не удалось ответить на /chatid в чат %s: %s", chat.get("id"), exc)
        pass  # подсказка — best-effort


def _display_name(user: dict) -> str:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return name or user.get("username") or str(user.get("id", "?"))


_ATTACHMENT_KEYS = ("photo", "document", "video", "voice", "audio", "sticker", "animation", "video_note")


def _ingest_board_chat(message: dict, text: str) -> bool:
    """Сообщение из группы правления (TelegramSettings.board_chat_id) -> чат
    правления на сайте (app/board_chat.py: ingest_external). True — это была
    группа правления (сообщение обработано, дальше не разбирать). Команды
    боту и сообщения других ботов в чат не попадают."""
    from . import board_chat
    if not board_chat.is_board_chat(board_chat.SOURCE_TELEGRAM, message["chat"]["id"]):
        return False
    sender = message.get("from") or {}
    if sender.get("is_bot") or text.startswith("/"):
        return True
    # Вложения на сайт не переносим (в чате правления их нет) — только
    # подпись и пометка, чтобы на сайте было видно, что что-то прислали.
    attachment = any(message.get(k) for k in _ATTACHMENT_KEYS)
    body = text or (message.get("caption") or "").strip()
    if message.get("sticker"):
        # Картинку стикера не переносим — только его эмодзи.
        body = f"[стикер {message['sticker'].get('emoji') or ''}]".replace(" ]", "]")
    elif attachment:
        body = f"[вложение в Telegram] {body}".strip()
    if not body:
        return True  # сервисное сообщение (вступил/вышел, закреп и т. п.)
    board_chat.ingest_external(
        board_chat.SOURCE_TELEGRAM, f"{message['chat']['id']}:{message['message_id']}",
        sender.get("id"), _display_name(sender), body,
    )
    return True


def _log_update(update: dict, message: dict | None) -> None:
    """Строка в лог cron на каждый апдейт — тип, чат и (для команд) сама
    команда; обычный текст не пишем (переписка правления в логе ни к чему).
    Без этого «бот молчит» не отличить от «апдейт не пришёл»."""
    kind = next((k for k in update if k != "update_id"), "?")
    if not message:
        chat = ((update.get(kind) or {}) if isinstance(update.get(kind), dict) else {}).get("chat") or {}
        logger.info("Telegram: апдейт %s, чат %s", kind, chat.get("id"))
        return
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0] if text.startswith("/") else ""
    extra = ""
    if message.get("migrate_to_chat_id"):
        extra = f", группа стала супергруппой: новый ID {message['migrate_to_chat_id']}"
    logger.info("Telegram: апдейт %s, чат %s (%s)%s%s", kind, chat.get("id"), chat.get("type"),
                f", команда {command}" if command else "", extra)


def poll_and_link_accounts() -> tuple[int, int]:
    """Сама логика cron-скрипта scripts/poll_messengers.py (тонкая обёртка,
    тот же принцип, что у app/notifications.py:run_board_chat_digest и
    app/penalty.py:accrue_penalties). Возвращает (число апдейтов, число
    привязанных аккаунтов). Ничего не делает и не поднимает исключение,
    если бот не настроен — вызывающий сам решает, как это показать."""
    settings = database.db_session.query(TelegramSettings).first()
    if settings is None or not is_configured(settings):
        return 0, 0

    offset = (settings.last_update_id + 1) if settings.last_update_id is not None else None
    try:
        updates = get_updates(settings, offset=offset)
    except TelegramError as exc:
        settings.last_error = str(exc)
        database.db_session.commit()
        raise

    linked = 0
    for update in updates:
        settings.last_update_id = update["update_id"]
        message = update.get("message") or update.get("channel_post")
        _log_update(update, message)
        if not message:
            continue
        text = (message.get("text") or "").strip()
        if _is_chatid_command(text):
            _reply_chat_id(settings, message["chat"])
            continue
        if update.get("message") and _ingest_board_chat(message, text):
            continue
        if not text.startswith("/start "):
            continue
        token = text[len("/start "):].strip()
        if not token:
            continue

        person = database.db_session.query(Person).filter_by(telegram_link_token=token).first()
        if person is None:
            # Токен не найден — истёк/уже использован/подделан. Тихо
            # игнорируем: бот не даёт обратной связи по несуществующим
            # токенам, чтобы не помогать перебору.
            continue
        chat_id = message["chat"]["id"]
        person.telegram_chat_id = chat_id
        person.telegram_link_token = None
        linked += 1
        try:
            send_message(
                settings, chat_id,
                "Готово! Теперь вы будете получать здесь уведомления с сайта кооператива "
                "(если включите их в настройках профиля).",
            )
        except TelegramError:
            pass  # привязка уже сохранена, приветствие — best-effort

    settings.last_error = None
    settings.last_polled_at = dt.datetime.utcnow()
    database.db_session.commit()
    return len(updates), linked


# ---------------------------------------------------------------------------
# Дублирование новостей в группу/канал
# ---------------------------------------------------------------------------

# Bot API ограничивает сообщение 4096 символами, подпись к фото/альбому —
# 1024 (оба — после разбора HTML, в UTF-16). Текст длиннее NEWS_TEXT_LIMIT
# отправляем обрезанным без разметки, полная версия всё равно по ссылке.
NEWS_TEXT_LIMIT = 3500
NEWS_EXCERPT_LENGTH = 1500
CAPTION_LIMIT = 1024
# Лимиты Bot API на загрузку: фото до 10 МБ (больше — уходит документом),
# любой файл до 50 МБ (больше — не отправляем, предупреждаем).
PHOTO_MAX_BYTES = 10 * 1024 * 1024
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024
ALBUM_MAX_ITEMS = 10
UPLOAD_TIMEOUT = 60
_TAG_RE = re.compile(r"<[^>]+>")


def news_group_enabled(settings: TelegramSettings | None) -> bool:
    return bool(is_configured(settings) and settings.news_chat_id)


def _visible_length(html_text: str) -> int:
    """Длина текста так, как её считает Telegram: без тегов, с раскрытыми
    сущностями, в кодовых единицах UTF-16 (эмодзи — 2)."""
    plain = html.unescape(_TAG_RE.sub("", html_text))
    return len(plain.encode("utf-16-le")) // 2


_TAG_TOKEN_RE = re.compile(r"<(/?)([a-z-]+)[^>]*>")


def _balanced(html_text: str) -> bool:
    stack = []
    for closing, name in _TAG_TOKEN_RE.findall(html_text):
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


def _truncate_blocks(html_text: str, limit: int) -> str | None:
    """Обрезка длинной новости по границе абзаца (блоки разделены пустой
    строкой) с сохранением разметки — сколько целых абзацев влезает в limit,
    плюс «…»; полная версия — по ссылке. Режем только там, где все теги
    закрыты (не посреди цитаты/кода). None — даже первый абзац не влезает
    (тогда вызывающий шлёт обрезанный текст без разметки)."""
    blocks = html_text.split("\n\n")
    best = None
    for n in range(1, len(blocks) + 1):
        candidate = "\n\n".join(blocks[:n])
        if _visible_length(candidate) > limit:
            break
        if _balanced(candidate):
            best = candidate
    return best + "\n\n…" if best else None


def news_text(title: str, body: str, url: str) -> str:
    from markupsafe import escape
    from .news_format import telegram_html, excerpt

    rendered = telegram_html(body, base_url=url)
    if _visible_length(rendered) > NEWS_TEXT_LIMIT:
        rendered = _truncate_blocks(rendered, NEWS_TEXT_LIMIT) or str(escape(excerpt(body, NEWS_EXCERPT_LENGTH)[0]))
    return f"<b>{escape(title)}</b>\n\n{rendered}\n\n<a href=\"{escape(url)}\">Читать на сайте</a>"


def fits_caption(title: str, body: str, url: str = "https://example.org/news/0") -> bool:
    """Поместится ли новость подписью к альбому — одним постом. Тот же расчёт
    использует форма новости (news.telegram_layout), чтобы показывать выбор
    порядка «альбом/текст» только когда сообщений будет два."""
    return _visible_length(news_text(title, body, url)) <= CAPTION_LIMIT


def _split_attachments(item: News, upload_folder: str):
    """-> (фото для альбома, документы, пропущенные из-за размера). Фото —
    и вставленные в текст, и из галереи: картинки из текста в сообщение
    не попадают (telegram_html их выбрасывает), поэтому едут альбомом."""
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


def _upload(settings: TelegramSettings, method: str, data: dict, files: dict) -> None:
    token = _token(settings)
    handles = {}
    try:
        for field, (filename, path) in files.items():
            handles[field] = (filename, open(path, "rb"))
        resp = requests.post(_api(settings, token, method), data=data, files=handles,
                             timeout=UPLOAD_TIMEOUT, **_net(settings))
    except requests.RequestException as exc:
        raise _request_error(exc, idempotent=False) from exc
    except OSError as exc:  # файл вложения пропал с диска (InvalidSchema и пр. — уже RequestException выше)
        raise TelegramError(f"Файл вложения недоступен: {exc}", status_code=0) from exc
    finally:
        for _name, fh in handles.values():
            fh.close()
    if resp.status_code != 200:
        raise _status_error(resp)


def _album_steps(settings: TelegramSettings, photos, caption: str | None, silent: bool = False) -> list:
    """Одно фото — sendPhoto, больше — sendMediaGroup пачками по 10, каждая
    пачка — отдельный шаг. Подпись — у первого фото первой пачки (так Telegram
    показывает её под всем альбомом)."""
    chat_id = settings.news_chat_id
    base = {"chat_id": chat_id}
    if silent:
        base["disable_notification"] = "true"
    steps = []
    for start in range(0, len(photos), ALBUM_MAX_ITEMS):
        chunk = photos[start:start + ALBUM_MAX_ITEMS]
        with_caption = caption if start == 0 else None
        if len(chunk) == 1:
            att, path = chunk[0]
            data = dict(base)
            if with_caption:
                data.update(caption=with_caption, parse_mode="HTML")
            files = {"photo": (att.original_filename, path)}
            steps.append(lambda data=data, files=files: _upload(settings, "sendPhoto", data, files))
            continue
        media, files = [], {}
        for n, (att, path) in enumerate(chunk):
            entry = {"type": "photo", "media": f"attach://file{n}"}
            if n == 0 and with_caption:
                entry.update(caption=with_caption, parse_mode="HTML")
            media.append(entry)
            files[f"file{n}"] = (att.original_filename, path)
        data = {**base, "media": json.dumps(media)}
        steps.append(lambda data=data, files=files: _upload(settings, "sendMediaGroup", data, files))
    return steps


def news_steps(settings: TelegramSettings, item: News, url: str, upload_folder: str,
               album_first: bool = False, silent: bool = False) -> tuple[list, list[str]]:
    """Отправка новости в TelegramSettings.news_chat_id по шагам — каждый шаг
    это один запрос к Bot API (очередь, app/messenger_outbox.py, запоминает,
    сколько шагов прошло, и при повторе продолжает с места сбоя):
    - нет фото — одно текстовое сообщение;
    - есть фото и текст влезает в подпись (CAPTION_LIMIT) — один пост,
      альбом с подписью;
    - текст длиннее — два сообщения, порядок задаёт album_first (галочка в
      форме новости);
    остальные вложения — следом, отдельными документами.
    silent — все сообщения без звука у участников группы.
    -> (шаги, имена файлов, пропущенных из-за лимитов Telegram)."""
    text = news_text(item.title, item.body, url)
    photos, documents, skipped = _split_attachments(item, upload_folder)

    def send_text():
        send_message(settings, settings.news_chat_id, text, parse_mode="HTML", disable_preview=True, silent=silent)

    if not photos:
        steps = [send_text]
    elif _visible_length(text) <= CAPTION_LIMIT:
        steps = _album_steps(settings, photos, caption=text, silent=silent)
    elif album_first:
        steps = _album_steps(settings, photos, caption=None, silent=silent) + [send_text]
    else:
        steps = [send_text] + _album_steps(settings, photos, caption=None, silent=silent)

    doc_data = {"chat_id": settings.news_chat_id}
    if silent:
        doc_data["disable_notification"] = "true"
    for att, path in documents:
        files = {"document": (att.original_filename, path)}
        steps.append(lambda files=files: _upload(settings, "sendDocument", doc_data, files))
    return steps, skipped


def publish_news(settings: TelegramSettings, item: News, url: str, upload_folder: str,
                 album_first: bool = False, silent: bool = False) -> list[str]:
    """Все шаги news_steps подряд, без очереди. -> пропущенные файлы."""
    steps, skipped = news_steps(settings, item, url, upload_folder, album_first, silent)
    for step in steps:
        step()
    return skipped
