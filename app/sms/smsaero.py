"""
Клиент SMS Aero (https://smsaero.ru/) — API v2, gate.smsaero.ru. Авторизация
HTTP Basic (email аккаунта + API-ключ, а не пароль от личного кабинета —
ключ выдаётся в настройках аккаунта SMS Aero). Формат запроса/ответа
подтверждён по официальным клиентским библиотекам SMS Aero (сама
документация — SPA без серверного рендера, не читается простым HTTP GET):
github.com/smsaero/smsaero_python (requests.post(url, json=data, ...)) и
github.com/smsaero/smsaero_c (Content-Type: application/json) — ТЕЛО
ЗАПРОСА ДОЛЖНО БЫТЬ JSON, не application/x-www-form-urlencoded (см. send()
ниже — раньше здесь было requests.post(..., data=data, ...), из-за чего
API v2 отвечал {"success": false, "message": "Validation error."} на
любую отправку, не имея возможности разобрать form-encoded тело).
"""
from __future__ import annotations

import requests

from .base import SmsClient, SmsError

API_URL = "https://gate.smsaero.ru/v2/sms/send"
REQUEST_TIMEOUT = 15  # секунд — тот же порядок, что и у mail_client.CONNECT_TIMEOUT


class SmsAeroClient(SmsClient):
    def __init__(self, email: str, api_key: str, sign: str | None = None):
        self.email = email
        self.api_key = api_key
        self.sign = sign

    def send(self, phone_digits: str, text: str) -> None:
        # SMS Aero принимает номер с кодом страны, без "+" (см. base.py:
        # SmsClient.send — phone_digits приходит 10-значным, без кода
        # страны, тут достраиваем "7", как везде в проекте для российских
        # номеров).
        number = f"7{phone_digits}"
        data = {"number": number, "text": text}
        if self.sign:
            data["sign"] = self.sign

        try:
            resp = requests.post(
                API_URL, json=data, auth=(self.email, self.api_key), timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SmsError(f"не удалось связаться с SMS Aero: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SmsError(f"SMS Aero вернул нераспознаваемый ответ (код {resp.status_code})") from exc

        if not payload.get("success"):
            raise SmsError(payload.get("message") or f"SMS Aero отклонил отправку (код {resp.status_code})")
