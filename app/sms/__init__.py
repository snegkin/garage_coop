"""
Фабрика клиента СМС-провайдера — единственное место, которое знает, какие
провайдеры реально поддержаны (см. app/bank_api/__init__.py — тот же
приём для банков). app/auth.py и app/sms_settings.py вызывают только
get_sms_client() и не импортируют конкретные клиенты напрямую.

Добавление нового агрегатора: реализовать SmsClient в отдельном модуле
рядом с smsaero.py, добавить новое значение в models.SmsProvider (своя
миграция на поля с реквизитами нового провайдера) и обработать его здесь.
"""
from __future__ import annotations

import re

from .. import database
from ..models import SmsSettings, SmsProvider, SmsLog, SmsLogStatus, Cooperative
from ..bank_api import crypto
from .base import SmsClient, SmsError
from .smsaero import SmsAeroClient

_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)


def sms_site_identifier(coop: Cooperative | None) -> str:
    """Название кооператива или адрес его сайта — для текста SMS с кодом
    подтверждения. Требование SMS Aero (и операторов связи вообще) при
    отправке от чужого/бесплатного имени отправителя ("SMS Aero" по
    умолчанию, см. smsaero.py): сообщение с кодом должно содержать
    название компании/системы или адрес сайта — просто "Код: 1234" без
    этого операторы блокируют. Сайт — БЕЗ протокола (http(s)://): ссылка
    с протоколом тоже под запретом — без него на смартфонах она всё
    равно остаётся кликабельной, а с ним считается подозрительной.
    Сайт предпочтительнее просто имени, если он указан в реквизитах."""
    if coop and coop.website:
        return _PROTOCOL_RE.sub("", coop.website).rstrip("/")
    if coop and (coop.short_name or coop.full_name):
        return coop.short_name or coop.full_name
    return ""


class _LoggingSmsClient(SmsClient):
    """Оборачивает любой SmsClient — пишет в SmsLog каждую попытку
    отправки (успех/ошибка) и сразу коммитит ТЕКУЩУЮ сессию (database.
    db_session), независимо от исхода. Если бы запись просто лежала в
    сессии до конца запроса, её стирал бы rollback() вызывающей стороны
    при ошибке отправки (см. auth.py: register_phone_confirm/
    forgot_password) — а именно неудачные отправки важнее всего видеть
    в журнале при разборе жалоб «SMS не приходят».

    ВАЖНО: коммитится вся сессия целиком, а не только запись лога — в
    SQLite нельзя писать логи через отдельное соединение, пока по
    основному есть незакоммиченная запись (только один писатель
    одновременно, даже в WAL-режиме). На практике это безобидно: во всех
    реальных вызовах (auth.py) к этому моменту в сессии есть только сам
    только что созданный одноразовый код (verification.issue_code) —
    его коммит независимо от исхода отправки не проблема (код и так
    хранится хэшем, истекает через 10 минут и заменяется при повторной
    попытке). См. tests/test_sms_client.py:
    test_failed_send_log_survives_caller_rollback."""

    def __init__(self, inner: SmsClient):
        self._inner = inner

    def send(self, phone_digits: str, text: str) -> None:
        error = None
        try:
            self._inner.send(phone_digits, text)
        except SmsError as exc:
            error = str(exc)
            raise
        finally:
            database.db_session.add(SmsLog(
                phone=phone_digits, text=text,
                status=SmsLogStatus.FAILED if error else SmsLogStatus.SENT,
                error=error,
            ))
            database.db_session.commit()


def get_sms_client(settings: SmsSettings | None) -> SmsClient | None:
    """None — интеграция не настроена (нет записи настроек, не выбран
    провайдер, не заполнены обязательные реквизиты) — вызывающий код
    показывает понятное сообщение вместо падения."""
    if settings is None or settings.provider != SmsProvider.SMSAERO:
        return None
    if not settings.smsaero_email or not settings.smsaero_api_key_encrypted:
        return None
    api_key = crypto.decrypt(settings.smsaero_api_key_encrypted)
    if not api_key:
        return None
    return _LoggingSmsClient(SmsAeroClient(settings.smsaero_email, api_key, settings.sender_sign or None))
