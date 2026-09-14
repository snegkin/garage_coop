"""
Клиент баланса хостинг-аккаунта Beget.com — метод user/getAccountInfo.
Эндпоинт/формат подтверждены официальной документацией (SPA на самом
beget.com не отдаёт исходники метода простым HTTP GET, поэтому — по
описанию, не по догадке):
beget.com/en/kb/api/basic-principles-of-operation-with-api (базовый URL,
параметры, обёртка ответа/ошибок), beget.com/ru/kb/api/funkczii-upravleniya-akkauntom
(поле user_balance у getAccountInfo).

GET https://api.beget.com/api/user/getAccountInfo?login=...&passwd=...&output_format=json
Ответ:
  {"status": "success", "answer": {"status": "success", "result": {"user_balance": 123.45, ...}}}
Ошибка — на ДВУХ возможных уровнях:
  верхний:  {"status": "error", "error_text": "...", "error_code": "..."}
  вложенный: {"status": "success", "answer": {"status": "error", "errors": [{"error_text": "...", ...}, ...]}}

ВАЖНО: пароль передаётся GET-параметром — при любой ошибке текст исключения
собирается ТОЛЬКО из тела ответа, никогда из resp.url/resp.request (там
пароль в открытом виде, пусть и по HTTPS) — иначе он может утечь в
flash-сообщение или лог.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import requests

from ..bank_api.base import BalanceInfo
from .base import CounterpartyApiClient, CounterpartyApiError

API_URL = "https://api.beget.com/api/user/getAccountInfo"
REQUEST_TIMEOUT = 15  # секунд — тот же порядок, что и у остальных клиентов проекта


class BegetBalanceClient(CounterpartyApiClient):
    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password

    def get_balance(self) -> BalanceInfo:
        try:
            resp = requests.get(
                API_URL, params={"login": self.login, "passwd": self.password, "output_format": "json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CounterpartyApiError(f"не удалось связаться с Beget: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise CounterpartyApiError(f"Beget вернул нераспознаваемый ответ (код {resp.status_code})") from exc

        if payload.get("status") != "success":
            message = payload.get("error_text") or f"Beget отклонил запрос (код {resp.status_code})"
            raise CounterpartyApiError(message)

        answer = payload.get("answer") or {}
        if answer.get("status") != "success":
            errors = answer.get("errors") or []
            if errors:
                message = "; ".join(e.get("error_text", "") for e in errors if e.get("error_text"))
            else:
                message = "Beget вернул ошибку без описания"
            raise CounterpartyApiError(message)

        result = answer.get("result") or {}
        balance = result.get("user_balance")
        if balance is None:
            raise CounterpartyApiError("Beget не вернул значение баланса в ответе")

        return BalanceInfo(amount=Decimal(str(balance)), as_of=dt.date.today())
