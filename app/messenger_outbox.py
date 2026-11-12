"""
Очередь исходящих сообщений в мессенджеры (модель MessengerOutbox):
Telegram (app/telegram_bot.py) — новости и уведомления, MAX
(app/max_bot.py) — только новости. channel записи выбирает модуль бота;
у каждого одинаковый набор: get_settings, is_configured, news_steps,
ошибки — app/messenger_errors.py.

Зачем: до api.telegram.org с сервера в РФ ходим через ретранслятор/прокси
(TelegramSettings.api_base_url/proxy_url_encrypted), и любая из этих точек
может на минуту-другую пропасть. Без очереди новость в группу или
уведомление в такой момент просто терялись. Теперь:
  - news.create / notifications._send ставят сообщение в очередь
    (enqueue_news/enqueue_message) и сразу возвращаются — сохранение новости
    не ждёт загрузку фото;
  - scripts/poll_telegram.py (cron раз в минуту) вызывает process():
    отправляет всё, чему подошёл срок, при сбое — повтор с нарастающей
    паузой (RETRY_DELAYS), через MAX_AGE — отказ.

Пост новости можно отправить без звука (silent) и отложенно (scheduled_at —
первая попытка не раньше этого момента; на /telegram/ виден как
«запланировано», там же отменяется).

Новость отправляется по шагам (telegram_bot.news_steps: текст, альбомы,
документы), steps_done — сколько уже ушло: повтор продолжает с места сбоя.

Повтор только там, где он безопасен:
  - запрос не дошёл до сервиса (MessengerError.transport) — повтор, а
    остальные записи этого мессенджера в этот запуск не трогаем (сеть
    явно лежит), другой мессенджер отправляется как обычно;
  - сервис ответил 429/5xx — повтор;
  - сервис ответил 400/403/404 (неверный chat_id, бота убрали из группы,
    ошибка разметки) — сразу отказ: повтор не поможет;
  - запрос ушёл, ответа нет (MessengerUncertain) — статус «неизвестно»,
    решает правление на странице настроек мессенджера: автоматический
    повтор мог бы задвоить пост в группе.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import database
from . import max_bot
from . import telegram_bot
from .messenger_errors import MessengerError, MessengerUncertain
from .models import News, MessengerOutbox, MessengerOutboxStatus

logger = logging.getLogger(__name__)

# Паузы между попытками (минуты): 1, 2, 5, 10, 30, дальше — раз в час.
RETRY_DELAYS = [1, 2, 5, 10, 30, 60]
# Дольше суток не доставили — отказ (уведомление «завтра» уже не нужно,
# новость — правление увидит в списке на /telegram/ и повторит руками).
MAX_AGE = dt.timedelta(hours=24)
# За один запуск cron — не больше стольких записей (запуск раз в минуту,
# остальное — в следующий).
BATCH = 30
PERMANENT_STATUS_CODES = {0, 400, 403, 404}

KIND_MESSAGE = "message"
KIND_NEWS = "news"

CHANNEL_TELEGRAM = "telegram"
CHANNEL_MAX = "max"
BOTS = {CHANNEL_TELEGRAM: telegram_bot, CHANNEL_MAX: max_bot}
LABELS = {CHANNEL_TELEGRAM: "Telegram", CHANNEL_MAX: "MAX"}


def enqueue_message(chat_id: int | str, text: str) -> MessengerOutbox:
    """Без commit — вызывающий коммитит вместе со своим изменением."""
    item = MessengerOutbox(kind=KIND_MESSAGE, chat_id=str(chat_id), text=text)
    database.db_session.add(item)
    return item


def enqueue_news(news: News, url: str, album_first: bool = False, silent: bool = False,
                 send_at: dt.datetime | None = None, channel: str = CHANNEL_TELEGRAM) -> MessengerOutbox:
    """send_at (UTC) — отложенная отправка; None или прошедший момент — с
    ближайшим запуском cron."""
    now = dt.datetime.utcnow()
    scheduled = send_at if send_at and send_at > now else None
    item = MessengerOutbox(
        kind=KIND_NEWS, channel=channel, news=news, url=url, album_first=album_first, silent=silent,
        scheduled_at=scheduled, next_attempt_at=scheduled or now,
    )
    database.db_session.add(item)
    return item


def _retry_delay(attempts: int) -> dt.timedelta:
    return dt.timedelta(minutes=RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1])


def _send(bot, settings, item: MessengerOutbox, upload_folder: str) -> None:
    if item.kind == KIND_MESSAGE:
        bot.send_message(settings, item.chat_id, item.text)
        return
    if item.news is None:
        raise MessengerError("Новость удалена с сайта", status_code=0)
    if not settings.news_chat_id:
        raise MessengerError("Группа для новостей не задана в настройках", status_code=0)
    steps, _skipped = bot.news_steps(
        settings, item.news, item.url, upload_folder, item.album_first, item.silent,
    )
    for step in steps[item.steps_done:]:
        step()
        item.steps_done += 1
        database.db_session.commit()  # шаг ушёл — при сбое следующего не повторять его


def process(upload_folder: str, now: dt.datetime | None = None) -> tuple[int, int]:
    """Отправляет записи, которым подошёл срок. -> (отправлено, не удалось).
    Записи мессенджера, бот которого не настроен, ждут настройки."""
    now = now or dt.datetime.utcnow()
    items = (
        database.db_session.query(MessengerOutbox)
        .filter(MessengerOutbox.status == MessengerOutboxStatus.PENDING, MessengerOutbox.next_attempt_at <= now)
        .order_by(MessengerOutbox.id)
        .limit(BATCH)
        .all()
    )
    settings_by_channel: dict = {}
    down: set[str] = set()  # сервис недоступен — его записи до следующего запуска не трогаем
    sent = failed = 0
    for item in items:
        bot = BOTS.get(item.channel)
        if bot is None or item.channel in down:
            continue
        if item.channel not in settings_by_channel:
            settings_by_channel[item.channel] = bot.get_settings()
        settings = settings_by_channel[item.channel]
        if not bot.is_configured(settings):
            continue
        item.attempts += 1
        try:
            _send(bot, settings, item, upload_folder)
        except MessengerUncertain as exc:
            item.status = MessengerOutboxStatus.UNCERTAIN
            item.last_error = str(exc)
            failed += 1
        except MessengerError as exc:
            item.last_error = str(exc)
            failed += 1
            due_since = max(item.created_at, item.scheduled_at or item.created_at)
            if exc.status_code in PERMANENT_STATUS_CODES or now - due_since > MAX_AGE:
                item.status = MessengerOutboxStatus.FAILED
            else:
                item.next_attempt_at = now + _retry_delay(item.attempts)
            if exc.transport:
                down.add(item.channel)
        else:
            item.status = MessengerOutboxStatus.SENT
            item.sent_at = dt.datetime.utcnow()
            item.last_error = None
            sent += 1
        database.db_session.commit()
    return sent, failed


def problems(channel: str, limit: int = 50) -> list[MessengerOutbox]:
    """Для страниц настроек (/telegram/, /max/): всё, что ещё не доставлено
    или требует решения."""
    return (
        database.db_session.query(MessengerOutbox)
        .filter(MessengerOutbox.channel == channel, MessengerOutbox.status.in_([
            MessengerOutboxStatus.PENDING, MessengerOutboxStatus.UNCERTAIN, MessengerOutboxStatus.FAILED,
        ]))
        .order_by(MessengerOutbox.id.desc())
        .limit(limit)
        .all()
    )


def retry(item: MessengerOutbox) -> None:
    """«Отправить повторно»: для новости — с того шага, на котором
    оборвалось (шаг с неизвестным исходом отправится ещё раз — правление
    так решило)."""
    item.status = MessengerOutboxStatus.PENDING
    item.next_attempt_at = dt.datetime.utcnow()
    item.created_at = dt.datetime.utcnow()  # заново отсчитывать MAX_AGE
    item.scheduled_at = None  # «повторно» — значит сейчас, а не в старое запланированное время


def mark_sent(item: MessengerOutbox) -> None:
    item.status = MessengerOutboxStatus.SENT
    item.sent_at = dt.datetime.utcnow()


def cancel(item: MessengerOutbox) -> None:
    item.status = MessengerOutboxStatus.CANCELLED
