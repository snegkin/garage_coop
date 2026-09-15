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

from decimal import Decimal

import requests

from .base import SmsClient, SmsError

API_URL = "https://gate.smsaero.ru/v2/sms/send"
STATUS_API_URL = "https://gate.smsaero.ru/v2/sms/status"  # см. get_message_cost — эндпоинт не подтверждён живым запросом
REQUEST_TIMEOUT = 15  # секунд — тот же порядок, что и у mail_client.CONNECT_TIMEOUT


class SmsAeroClient(SmsClient):
    def __init__(self, email: str, api_key: str, sign: str | None = None):
        self.email = email
        self.api_key = api_key
        self.sign = sign

    def send(self, phone_digits: str, text: str) -> str | None:
        # SMS Aero принимает номер с кодом страны, без "+" (см. base.py:
        # SmsClient.send — phone_digits приходит 10-значным, без кода
        # страны, тут достраиваем "7", как везде в проекте для российских
        # номеров).
        number = f"7{phone_digits}"
        # "sign" — ОБЯЗАТЕЛЬНОЕ поле API (проверено на реальном аккаунте:
        # без него запрос падает с {"success": false, "data": {"sign":
        # ["required"]}, "message": "Validation error."}), а не
        # опциональное, как предполагалось изначально. Если председатель
        # не зарегистрировал/не указал своё имя отправителя в настройках
        # (SmsSettings.sender_sign), подставляем "SMS Aero" — встроенный
        # дефолт самого провайдера для аккаунтов без своей подписи (см.
        # official-клиент smsaero/smsaero_python: SIGNATURE = "SMS Aero").
        data = {"number": number, "text": text, "sign": self.sign or "SMS Aero"}

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
            message = payload.get("message") or f"SMS Aero отклонил отправку (код {resp.status_code})"
            # payload["data"] при 400 Validation error — {"поле": ["ошибка",
            # ...], ...} по каждому невалидному полю сразу (не только
            # первому) — без этого текст ошибки был просто "Validation
            # error." без единой зацепки, какое поле не так (реальный
            # случай, из-за которого нашли требование "sign" выше).
            errors = payload.get("data")
            if isinstance(errors, dict) and errors:
                details = "; ".join(
                    f"{field}: {', '.join(field_errors)}" for field, field_errors in errors.items()
                )
                message = f"{message} ({details})"
            raise SmsError(message)

        # "data.id" — id сообщения в системе SMS Aero; по нему позже
        # запрашивается реальная стоимость отправки (см. get_message_cost
        # ниже) — сам send() её не возвращает. int(...) в ответе, приводим
        # к str — id используется только как непрозрачный идентификатор
        # для последующего запроса, не как число.
        message_id = payload.get("data", {}).get("id")
        return str(message_id) if message_id is not None else None

    def get_message_cost(self, message_id: str) -> Decimal | None:
        """
        Реальная стоимость уже отправленного сообщения (₽) — для начисления
        на счёт получателя платной SMS (см. scripts/reconcile_sms_charges.py,
        FeeType "telecom_disputes"). Стоимость в ответе send() выше не
        приходит, поэтому её узнают отдельным запросом ПОЗЖЕ, когда
        провайдер её посчитает (обычно с задержкой).

        ВНИМАНИЕ: эндпоинт и формат ответа НЕ подтверждены живым запросом
        (в отличие от send()/app/counterparty_api/smsaero.py:get_balance —
        см. их докстринги про сверку с github.com/smsaero/smsaero_python),
        только по документированной структуре API v2 SMS Aero — тот же
        envelope {"success", "data", "message"}, что у send()/balance(),
        путь /v2/sms/status, тело {"id": message_id}, ожидаемое поле
        data.cost. Перед тем как полагаться на автоматическое начисление
        в проде, стоит проверить это реальным запросом на боевом аккаунте
        и поправить при расхождении.
        """
        try:
            resp = requests.post(
                STATUS_API_URL, json={"id": message_id}, auth=(self.email, self.api_key), timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SmsError(f"не удалось связаться с SMS Aero: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SmsError(f"SMS Aero вернул нераспознаваемый ответ (код {resp.status_code})") from exc

        if not payload.get("success"):
            raise SmsError(payload.get("message") or f"SMS Aero отклонил запрос статуса (код {resp.status_code})")

        cost = payload.get("data", {}).get("cost")
        return Decimal(str(cost)) if cost is not None else None
