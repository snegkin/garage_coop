"""
Неофициальный клиент облака eWeLink для опроса устройств Sonoff POWCT.

Общий интерфейс для app/electricity_monitor.py и scripts/poll_ewelink.py, по
аналогии с app/bank_api/base.py — та же идея: маршруты/скрипт не должны
знать деталей HTTP-протокола конкретного облака.

**Важно, в отличие от app/bank_api/sberbank.py: это НЕ официальный API.**
На этапе постановки задачи сознательно выбран неофициальный вход по
email/паролю (как в клиентах Home Assistant SonoffLAN / ewelink-api) вместо
официального OAuth2 Open API (dev.ewelink.cc) — модерация заявки там может
занимать до нескольких дней. Логика ниже основана на публично известном
поведении сторонних клиентов, а НЕ на официальной документации, и поэтому
помечена как неподтверждённая живым тестом до первого реального запроса к
устройствам заказчика. После первого успешного запроса зафиксировать в
context.md/README.md:
  - реальный host региона (REGION_HOSTS ниже может быть устаревшим);
  - путь и формат запроса обновления токена (_refresh: путь эндпоинта не
    подтверждён, есть минимум два варианта в разных клиентах);
  - точные имена полей params для POWCT (см. PARAM_KEY_CANDIDATES ниже) —
    у разных прошивок отличаются.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation

import requests

# Известные региональные хосты HTTP API (v2, домен coolkit.cc/coolkit.cn).
# НЕ ПОДТВЕРЖДЕНО живым запросом — eWeLink периодически меняет поддомены
# (apia / api), список собран по нескольким open-source клиентам.
REGION_HOSTS = {
    "eu": "https://eu-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "as": "https://as-apia.coolkit.cc",
    "cn": "https://cn-apia.coolkit.cn",
}
DEFAULT_REGION = "eu"

# POWCT в разных прошивках отдаёт мощность/напряжение/ток либо без суффикса
# (однофазная логика на одно устройство — наш случай, т.к. на фазу по
# отдельному устройству), либо с суффиксом _00 (многоканальные версии).
# Список кандидатов проверяется по порядку при разборе ответа устройства —
# см. _extract_reading(). ПРОВЕРИТЬ и, если нужно, дополнить по факту
# реального ответа устройств заказчика.
POWER_KEYS = ("power", "power_00", "actPow_00")
VOLTAGE_KEYS = ("voltage", "voltage_00")
CURRENT_KEYS = ("current", "current_00")


class EWeLinkApiError(Exception):
    """Любая ошибка обращения к облаку eWeLink — сетевая, авторизации, формата
    ответа. Вызывающий код (electricity_monitor.py, poll_ewelink.py) ловит
    именно этот тип и сохраняет str(e) в EWeLinkAccount.last_error, не давая
    упасть всей синхронизации целиком."""


class EWeLinkAuthError(EWeLinkApiError):
    """Отдельный подкласс для протухшего/невалидного токена — чтобы вызывающий
    код мог отличить 'нужен login()/refresh()' от 'устройство офлайн' или
    временной сетевой ошибки."""


@dataclasses.dataclass
class EWeLinkTokens:
    access_token: str
    refresh_token: str
    region: str
    obtained_at: float


@dataclasses.dataclass
class PhaseSnapshot:
    """Результат разбора params одного устройства POWCT."""
    power_w: Decimal | None
    voltage_v: Decimal | None
    current_a: Decimal | None
    is_online: bool
    raw_params: dict


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _first_present(params: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key in params:
            return _to_decimal(params[key])
    return None


def parse_phase_snapshot(device: dict) -> PhaseSnapshot:
    """Разбирает один элемент из thingList (ответ /v2/device/thing) в снимок
    показаний. params с числами часто приходят домноженными на 100 у eWeLink
    (например, voltage=22050 значит 220.50 В) — ПРОВЕРИТЬ на реальном
    устройстве: если после первого живого теста окажется, что значения
    домножены, поделить здесь на 100, а не в вызывающем коде."""
    item = device.get("itemData", device)
    params = item.get("params", {}) or {}
    online = bool(item.get("online", params.get("online", True)))
    return PhaseSnapshot(
        power_w=_first_present(params, POWER_KEYS),
        voltage_v=_first_present(params, VOLTAGE_KEYS),
        current_a=_first_present(params, CURRENT_KEYS),
        is_online=online,
        raw_params=params,
    )


class EWeLinkClient:
    """
    Использование:

        client = EWeLinkClient(app_id, app_secret, email=..., password=...)
        tokens = client.login()                 # один раз, сохранить в БД
        ...
        client = EWeLinkClient(app_id, app_secret, tokens=tokens)
        try:
            devices = client.list_devices()
        except EWeLinkAuthError:
            tokens = client.refresh()            # сохранить новые токены в БД!
            devices = client.list_devices()

    Вызывающий код обязан сохранять новые токены сразу после успешного
    refresh()/login(), даже если последующий запрос устройства упадёт — тот
    же принцип, что и для Sberbank (см. app/bank_sync.py:
    _persist_rotated_refresh_token).
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        email: str | None = None,
        password: str | None = None,
        tokens: EWeLinkTokens | None = None,
        region: str = DEFAULT_REGION,
        timeout: float = 10.0,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.email = email
        self.password = password
        self.tokens = tokens
        self.region = tokens.region if tokens else region
        self.timeout = timeout
        # Если во время refresh() eWeLink вернул новый refresh_token взамен
        # старого — сюда, чтобы вызывающий код (poll_ewelink.py) его сохранил.
        self.rotated_refresh_token: str | None = None

    # ---------- низкоуровневые запросы ----------

    def _sign(self, body: dict) -> str:
        payload = json.dumps(body, separators=(",", ":")).encode()
        digest = hmac.new(self.app_secret.encode(), payload, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _host(self) -> str:
        return REGION_HOSTS.get(self.region, REGION_HOSTS[DEFAULT_REGION])

    def _headers(self, body: dict, authorized: bool) -> dict:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "X-CK-Appid": self.app_id,
        }
        if authorized:
            if not self.tokens:
                raise EWeLinkAuthError("Нет токена доступа — сначала login()")
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"
        else:
            headers["Authorization"] = f"Sign {self._sign(body)}"
        return headers

    def _request(self, method: str, path: str, body: dict, authorized: bool) -> dict:
        url = f"{self._host()}{path}"
        try:
            resp = requests.request(
                method, url,
                json=body if method != "GET" else None,
                params=body if method == "GET" else None,
                headers=self._headers(body, authorized=authorized),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise EWeLinkApiError(f"Сетевая ошибка при обращении к eWeLink ({path}): {exc}") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise EWeLinkApiError(f"Некорректный ответ eWeLink ({path}), код {resp.status_code}: {exc}") from exc

        error = data.get("error", 0)
        if error != 0:
            msg = data.get("msg", "unknown error")
            # 401/402 — коды невалидного/протухшего токена в других open-source
            # клиентах eWeLink; НЕ ПОДТВЕРЖДЕНО живым тестом с этим приложением.
            if error in (401, 402):
                raise EWeLinkAuthError(f"eWeLink: токен недействителен (код {error}): {msg}")
            raise EWeLinkApiError(f"eWeLink error {error}: {msg}")
        return data

    # ---------- аутентификация ----------

    def login(self) -> EWeLinkTokens:
        if not self.email or not self.password:
            raise ValueError("email и password обязательны для login()")

        body = {"email": self.email, "password": self.password, "countryCode": "+7"}
        data = self._request("POST", "/v2/user/login", body, authorized=False)

        new_region = data.get("data", {}).get("region")
        if new_region and new_region != self.region:
            self.region = new_region
            data = self._request("POST", "/v2/user/login", body, authorized=False)

        payload = data["data"]
        self.tokens = EWeLinkTokens(
            access_token=payload["at"],
            refresh_token=payload["rt"],
            region=self.region,
            obtained_at=time.time(),
        )
        return self.tokens

    def refresh(self) -> EWeLinkTokens:
        """ПРОВЕРИТЬ живым запросом: путь эндпоинта и формат тела. Если после
        первого реального теста окажется, что путь другой (например,
        /v2/user/refresh/token) — поправить здесь и зафиксировать в
        context.md, как это уже делалось для Sberbank."""
        if not self.tokens:
            raise EWeLinkAuthError("Нет текущих токенов для refresh()")
        body = {"rt": self.tokens.refresh_token}
        data = self._request("POST", "/v2/user/refresh", body, authorized=False)
        payload = data["data"]
        new_refresh = payload.get("rt")
        if new_refresh and new_refresh != self.tokens.refresh_token:
            self.rotated_refresh_token = new_refresh
        self.tokens = EWeLinkTokens(
            access_token=payload["at"],
            refresh_token=new_refresh or self.tokens.refresh_token,
            region=self.region,
            obtained_at=time.time(),
        )
        return self.tokens

    # ---------- устройства ----------

    def list_devices(self) -> list[dict]:
        data = self._request("GET", "/v2/device/thing", {}, authorized=True)
        return data.get("data", {}).get("thingList", [])

    def get_phase_snapshot(self, ewelink_device_id: str, devices: list[dict] | None = None) -> PhaseSnapshot:
        """devices можно передать заранее полученным списком (list_devices()),
        чтобы не делать отдельный запрос на каждую из 3 фаз — см.
        scripts/poll_ewelink.py, который запрашивает список один раз на
        весь цикл опроса."""
        if devices is None:
            devices = self.list_devices()
        for d in devices:
            item = d.get("itemData", d)
            if item.get("deviceid") == ewelink_device_id:
                return parse_phase_snapshot(d)
        raise EWeLinkApiError(f"Устройство {ewelink_device_id} не найдено в списке eWeLink")
