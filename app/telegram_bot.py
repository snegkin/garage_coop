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
  3. scripts/poll_telegram.py (cron, long polling через getUpdates) видит
     этот апдейт, находит Person по токену, сохраняет chat_id, сбрасывает
     токен (одноразовый) и отвечает приветственным сообщением.

Реальная отправка уведомлений (notifications.py: notify) использует уже
сохранённый chat_id — Person.telegram (свободный текст "вот мой аккаунт"
для отображения на карточке) в этом не участвует.
"""
import datetime as dt

import requests

from . import database
from .bank_api import crypto
from .models import News, Person, TelegramSettings

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 10


class TelegramError(Exception):
    pass


def get_settings() -> TelegramSettings | None:
    return database.db_session.query(TelegramSettings).first()


def _token(settings: TelegramSettings) -> str:
    token = crypto.decrypt(settings.bot_token_encrypted)
    if not token:
        raise TelegramError("Токен бота не задан")
    return token


def is_configured(settings: TelegramSettings | None) -> bool:
    return bool(settings and settings.bot_token_encrypted and settings.bot_username)


def send_message(settings: TelegramSettings, chat_id: int | str, text: str,
                 parse_mode: str | None = None, disable_preview: bool = False) -> None:
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
    try:
        resp = requests.post(
            f"{API_BASE}/bot{token}/sendMessage",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram: {exc}") from exc
    if resp.status_code != 200:
        raise TelegramError(f"Telegram: HTTP {resp.status_code}: {resp.text[:200]}")


def get_updates(settings: TelegramSettings, offset: int | None, timeout: int = 0) -> list[dict]:
    """timeout=0 — обычный short-poll (используется из cron раз в минуту,
    держать HTTP-соединение открытым долгим long-poll'ом внутри cron-скрипта
    смысла не имеет — следующий запуск всё равно через минуту)."""
    token = _token(settings)
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(f"{API_BASE}/bot{token}/getUpdates", params=params, timeout=REQUEST_TIMEOUT + timeout)
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram: {exc}") from exc
    if resp.status_code != 200:
        raise TelegramError(f"Telegram: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"Telegram: {data.get('description', 'unknown error')}")
    return data.get("result", [])


def get_me(settings: TelegramSettings) -> dict:
    """Используется только на странице настроек — «Проверить подключение»
    (показывает имя бота, подтверждает, что токен рабочий)."""
    token = _token(settings)
    try:
        resp = requests.get(f"{API_BASE}/bot{token}/getMe", timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram: {exc}") from exc
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"Telegram: {data.get('description', 'unknown error')}")
    return data["result"]


def _is_chatid_command(text: str) -> bool:
    """/chatid или /chatid@имя_бота (так Telegram подставляет команду,
    выбранную из меню в группе)."""
    command = text.split(maxsplit=1)[0] if text else ""
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
            "(поле «Группа для новостей»).",
        )
    except TelegramError:
        pass  # подсказка — best-effort


def poll_and_link_accounts() -> tuple[int, int]:
    """Сама логика cron-скрипта scripts/poll_telegram.py (тонкая обёртка,
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
        if not message:
            continue
        text = (message.get("text") or "").strip()
        if _is_chatid_command(text):
            _reply_chat_id(settings, message["chat"])
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

# Bot API ограничивает сообщение 4096 символами (после разбора HTML) — берём
# с запасом под заголовок и ссылку; длиннее — отправляем обрезанный текст
# без разметки, полная версия всё равно по ссылке.
NEWS_TEXT_LIMIT = 3500
NEWS_EXCERPT_LENGTH = 1500


def news_group_enabled(settings: TelegramSettings | None) -> bool:
    return bool(is_configured(settings) and settings.news_chat_id)


def publish_news(settings: TelegramSettings, item: News, url: str) -> None:
    """Отправляет новость в TelegramSettings.news_chat_id. Поднимает
    TelegramError — вызывающий (news.create) показывает предупреждение, но
    сама новость на сайте к этому моменту уже сохранена."""
    from markupsafe import escape
    from .news_format import telegram_html, excerpt

    body = telegram_html(item.body, base_url=url)
    if len(body) > NEWS_TEXT_LIMIT:
        body = str(escape(excerpt(item.body, NEWS_EXCERPT_LENGTH)[0]))
    text = f"<b>{escape(item.title)}</b>\n\n{body}\n\n<a href=\"{escape(url)}\">Читать на сайте</a>"
    send_message(settings, settings.news_chat_id, text, parse_mode="HTML", disable_preview=True)
