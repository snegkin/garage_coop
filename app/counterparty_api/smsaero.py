"""
Клиент баланса личного кабинета SMS Aero (https://smsaero.ru/) — API v2,
gate.smsaero.ru. Тот же аккаунт и та же авторизация (HTTP Basic:
email + API-ключ), что и у app/sms/smsaero.py (отправка СМС) — но это
отдельный эндпоинт и отдельная библиотека применения (баланс контрагента
в разделе «Контрагенты», не отправка кода при входе), поэтому вынесен в
отдельный маленький клиент, а не смешан с SmsClient.

Эндпоинт подтверждён по исходнику официального клиента (SPA-документация
smsaero.ru не читается простым HTTP GET, см. комментарий в
app/sms/smsaero.py про тот же приём для send()):
github.com/smsaero/smsaero_python, smsaero/__init__.py, метод balance() —
POST https://gate.smsaero.ru/v2/balance, ответ
{"success": true, "data": {"balance": 337.03}, "message": ...}. Формат
ошибки — тот же, что и у /v2/sms/send (см. send() в app/sms/smsaero.py).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import requests

from ..bank_api.base import BalanceInfo
from .base import CounterpartyApiClient, CounterpartyApiError

API_URL = "https://gate.smsaero.ru/v2/balance"
REQUEST_TIMEOUT = 15  # секунд — тот же порядок, что и у app/sms/smsaero.py


class SmsAeroBalanceClient(CounterpartyApiClient):
    def __init__(self, email: str, api_key: str):
        self.email = email
        self.api_key = api_key

    def get_balance(self) -> BalanceInfo:
        try:
            resp = requests.post(API_URL, auth=(self.email, self.api_key), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise CounterpartyApiError(f"не удалось связаться с SMS Aero: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise CounterpartyApiError(f"SMS Aero вернул нераспознаваемый ответ (код {resp.status_code})") from exc

        if not payload.get("success"):
            message = payload.get("message") or f"SMS Aero отклонил запрос баланса (код {resp.status_code})"
            errors = payload.get("data")
            if isinstance(errors, dict) and errors:
                details = "; ".join(
                    f"{field}: {', '.join(field_errors)}" for field, field_errors in errors.items()
                )
                message = f"{message} ({details})"
            raise CounterpartyApiError(message)

        data = payload.get("data") or {}
        balance = data.get("balance")
        if balance is None:
            raise CounterpartyApiError("SMS Aero не вернул значение баланса в ответе")

        return BalanceInfo(amount=Decimal(str(balance)), as_of=dt.date.today())
