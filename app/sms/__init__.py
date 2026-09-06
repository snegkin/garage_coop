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

from ..models import SmsSettings, SmsProvider
from ..bank_api import crypto
from .base import SmsClient, SmsError
from .smsaero import SmsAeroClient


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
    return SmsAeroClient(settings.smsaero_email, api_key, settings.sender_sign or None)
