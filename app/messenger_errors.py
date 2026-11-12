"""
Общие ошибки отправки в мессенджеры (Telegram — app/telegram_bot.py, MAX —
app/max_bot.py). Очередь (app/messenger_outbox.py) решает по ним, повторять
ли отправку, не зная, какой это мессенджер.
"""
import requests


class MessengerError(Exception):
    """status_code — HTTP-код, если сервис ответил ошибкой (400 — неверный
    chat_id/разметка, 403 — бота убрали из группы, 429 — слишком часто);
    0 — ошибка на нашей стороне (новость удалена, файл пропал), повтор не
    поможет; transport=True — запрос до сервиса не дошёл (сеть, прокси,
    ретранслятор), повтор безопасен."""

    def __init__(self, message: str, status_code: int | None = None, transport: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.transport = transport


class MessengerUncertain(MessengerError):
    """Запрос ушёл, но ответа нет (таймаут чтения, обрыв соединения) —
    сервис мог его принять. Повтор может задвоить сообщение, поэтому
    очередь такое автоматически не повторяет."""


# Признаки сбоя ДО отправки запроса (не удалось соединиться) — повтор
# безопасен. Всё прочее после установленного соединения — неизвестность.
_CONNECT_STAGE_MARKERS = (
    "NewConnectionError", "Failed to establish a new connection", "Connection refused",
    "Network is unreachable", "NameResolutionError", "getaddrinfo", "ConnectTimeoutError",
)


def is_connect_stage(exc: requests.RequestException) -> bool:
    """Сбой случился до того, как запрос ушёл на сервер."""
    return isinstance(exc, (
        requests.ConnectTimeout, requests.exceptions.ProxyError, requests.exceptions.SSLError,
        requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
    )) or (not isinstance(exc, requests.ReadTimeout) and any(m in str(exc) for m in _CONNECT_STAGE_MARKERS))
