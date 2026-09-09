"""
Push-уведомления браузера (Web Push, RFC 8030 + VAPID/RFC 8292) — третий
канал доставки, наряду с email и Telegram (app/notifications.py).

В отличие от email/Telegram, у канала нет отдельной "настройки провайдера"
— ключи VAPID генерируются самим приложением при первом обращении
(get_or_create_settings), никакого внешнего аккаунта не нужно. Публичный
ключ передаётся клиентскому JS (не секрет — часть протокола, им браузер
подтверждает, что подписка создана для этого сервера), приватный хранится
зашифрованным (тот же Fernet, что и токен Telegram-бота/пароль почты, см.
app/bank_api/crypto.py).

У одного пользователя может быть несколько подписок (разные браузеры/
устройства, см. WebPushSubscription) — в отличие от единственного
telegram_chat_id/email, поэтому "готовность" канала — наличие хотя бы
одной подписки, а notify() рассылает во ВСЕ разом.

Подписка истекает на стороне браузера/push-сервиса без уведомления
сервера — единственный способ узнать об этом — получить 404/410 при
попытке отправки, тогда запись удаляется (см. is_expired(), вызывается
из notifications.notify)."""
import base64
import json

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02 as Vapid
from pywebpush import webpush, WebPushException

from . import database
from .bank_api import crypto
from .models import WebPushSettings, WebPushSubscription


class WebPushError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _b64urlencode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def get_or_create_settings() -> WebPushSettings:
    settings = database.db_session.query(WebPushSettings).first()
    if settings is not None and settings.public_key and settings.private_key_encrypted:
        return settings

    vapid = Vapid()
    vapid.generate_keys()
    public_key_b64 = _b64urlencode(
        vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )

    if settings is None:
        settings = WebPushSettings()
        database.db_session.add(settings)
    settings.public_key = public_key_b64
    settings.private_key_encrypted = crypto.encrypt(vapid.private_pem().decode("ascii"))
    # commit(), не flush() — вызывается и с обычных GET (см. cabinet.profile,
    # который сам ничего не коммитит), а ключи должны переживать конец
    # запроса, а не откатываться при db_session.remove() на teardown.
    database.db_session.commit()
    return settings


def set_subject(settings: WebPushSettings, subject: str) -> None:
    """subject — "mailto:..." или URL сайта, требование VAPID (см. модель).
    Отдельная функция, а не часть get_or_create_settings — subject меняется
    председателем в настройках отдельно от самих ключей."""
    settings.subject = subject
    database.db_session.commit()


def subscribe(user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
    """Идемпотентно — повторная подписка с тем же endpoint (например,
    браузер уже был подписан, пользователь нажал кнопку ещё раз) обновляет
    ключи, а не плодит дубли (endpoint уникален)."""
    existing = database.db_session.query(WebPushSubscription).filter_by(endpoint=endpoint).first()
    if existing is not None:
        existing.user_id = user_id
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        database.db_session.add(WebPushSubscription(
            user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth,
        ))
    database.db_session.commit()


def unsubscribe(user_id: int, endpoint: str) -> None:
    database.db_session.query(WebPushSubscription).filter_by(user_id=user_id, endpoint=endpoint).delete()
    database.db_session.commit()


def send(settings: WebPushSettings, subscription: WebPushSubscription, title: str, body: str) -> None:
    """Поднимает WebPushError при сбое — вызывающий код (notifications.notify)
    сам решает, что делать (обычно — залогировать, и при is_expired(exc)
    удалить подписку)."""
    private_key = crypto.decrypt(settings.private_key_encrypted)
    if not private_key:
        raise WebPushError("Ключи VAPID не настроены")
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps({"title": title, "body": body}, ensure_ascii=False),
            vapid_private_key=private_key,
            vapid_claims={"sub": settings.subject or "mailto:admin@example.com"},
        )
    except WebPushException as exc:
        raise WebPushError(str(exc), status_code=exc.status_code) from exc


def is_expired(exc: WebPushError) -> bool:
    """404/410 от push-сервиса — подписка больше не действительна (браузер
    отписался/данные устарели), а не временный сбой сети."""
    return exc.status_code in (404, 410)
