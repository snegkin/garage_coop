"""
Клиент официального eWeLink Open API (OAuth2, dev.ewelink.cc) для опроса
устройств Sonoff POWCT.

Общий интерфейс для app/electricity_monitor.py и scripts/poll_ewelink.py, по
аналогии с app/bank_api/base.py — та же идея: маршруты/скрипт не должны
знать деталей HTTP-протокола конкретного облака.

История: на этапе постановки задачи сознательно был выбран неофициальный
вход по email/паролю (как в клиентах Home Assistant SonoffLAN / ewelink-api)
вместо официального OAuth2 Open API — модерация заявки на dev.ewelink.cc
могла занять до нескольких дней. Приложение на dev.ewelink.cc одобрено,
поэтому модуль переписан на официальный authorization code flow — см.
EWeLinkAccount в app/models.py (email/password там больше нет, вместо них
app_id/app_secret — client id/secret из личного кабинета dev.ewelink.cc — и
family_id, выбранный после авторизации).

**Важно**: набор эндпоинтов (пути, POST@/v2/user/oauth/token и т.д.) задан
официальной документацией dev.ewelink.cc, но несколько деталей не покрыты
присланной таблицей эндпоинтов и взяты по памяти/косвенным источникам —
ПРОВЕРИТЬ живым тестом на первом реальном запуске и зафиксировать в
context.md/README.md:
  - точный формат подписи и параметров страницы авторизации в браузере
    (см. _authorize_signature/authorize_url) — это НЕ то же самое, что
    подпись тела JSON-запроса (см. _sign), у eWeLink это отдельная схема
    для HTML-страницы https://c2ccdn.coolkit.cc/oauth/index.html;
  - имя параметра family id в GET /v2/device/thing (сейчас "familyid");
  - реальный host региона (REGION_HOSTS ниже может быть устаревшим);
  - точные имена полей params для POWCT (см. PARAM_KEY_CANDIDATES ниже) —
    у разных прошивок отличаются;
  - имена полей токенов в ответах /v2/user/oauth/token и /v2/user/refresh —
    _extract_token_pair() принимает оба варианта написания (accessToken/at,
    refreshToken/rt), чтобы не упасть, какой бы ни оказался фактический.

Модуль реализует только чтение (список семей/устройств, снимок показаний) —
эндпоинты управления устройством (POST /v2/device/thing/status и
batch-status) в документации есть, но в этом приложении не нужны: раздел
«Мониторинг фаз» показывает показания, не переключает реле, поэтому клиент
их не реализует, чтобы не добавлять непроверенный код управления реальным
оборудованием без надобности.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

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

# Страница авторизации в браузере (redirect туда, eWeLink возвращает
# пользователя обратно на redirect_uri с ?code=...&region=...&state=...).
# ПРОВЕРИТЬ — см. предупреждение в начале файла.
AUTHORIZE_PAGE_URL = "https://c2ccdn.coolkit.cc/oauth/index.html"

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
    код мог отличить 'нужен refresh()/повторная авторизация' от 'устройство
    офлайн' или временной сетевой ошибки."""


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


def _extract_token_pair(payload: dict) -> tuple[str, str]:
    """Официальная документация даёт camelCase (accessToken/refreshToken);
    старые запросы к тому же backend иногда отвечали короткими at/rt —
    принимаем оба варианта, чтобы не гадать заранее, какой окажется на
    практике (см. предупреждение в начале файла)."""
    access_token = payload.get("accessToken") or payload.get("at")
    refresh_token = payload.get("refreshToken") or payload.get("rt")
    if not access_token or not refresh_token:
        raise EWeLinkApiError(f"eWeLink: не удалось разобрать токены в ответе: {payload!r}")
    return access_token, refresh_token


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
    Использование (authorization code flow):

        client = EWeLinkClient(app_id, app_secret)
        url = client.authorize_url(redirect_uri, state)   # редирект браузера сюда
        # ... eWeLink возвращает пользователя на redirect_uri?code=...&region=...
        tokens = client.exchange_code(code, redirect_uri)  # сохранить в БД
        families = client.list_families()                  # выбрать family_id, сохранить в БД
        ...
        client = EWeLinkClient(app_id, app_secret, tokens=tokens)
        try:
            devices = client.list_devices(family_id)
        except EWeLinkAuthError:
            tokens = client.refresh()            # сохранить новые токены в БД!
            devices = client.list_devices(family_id)

    Вызывающий код обязан сохранять новые токены сразу после успешного
    exchange_code()/refresh(), даже если последующий запрос устройства
    упадёт — тот же принцип, что и для Sberbank (см. app/bank_sync.py:
    _persist_rotated_refresh_token).
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        tokens: EWeLinkTokens | None = None,
        region: str = DEFAULT_REGION,
        timeout: float = 10.0,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
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

    def _authorize_signature(self, seq: str) -> str:
        """Подпись для страницы авторизации в браузере — ОТДЕЛЬНАЯ схема от
        _sign() (там подписывается JSON тела запроса). ПРОВЕРИТЬ по факту
        первой живой авторизации — см. предупреждение в начале файла."""
        payload = f"{self.app_id}_{seq}".encode()
        digest = hmac.new(self.app_secret.encode(), payload, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _host(self) -> str:
        return REGION_HOSTS.get(self.region, REGION_HOSTS[DEFAULT_REGION])

    def _headers(self, body: dict, authorized: bool) -> dict:
        headers = {
            # eWeLink валидирует значение заголовка точным совпадением строки
            # (Joi на бэкенде: "content-type" must be one of [application/json,
            # application/json; charset=utf-8]) — не MIME-парсингом, поэтому
            # пробел после ";" и регистр "utf-8" важны буквально.
            "Content-Type": "application/json; charset=utf-8",
            "X-CK-Appid": self.app_id,
        }
        if authorized:
            # Проверяем именно access_token, а не только наличие self.tokens:
            # self.tokens — датаклас, он truthy даже если поля внутри пустые
            # (например, вызывающий код расшифровал токен из БД неудачно и
            # положил None) — без этой проверки получился бы буквально
            # заголовок "Bearer None" вместо понятной ошибки.
            if not self.tokens or not self.tokens.access_token:
                raise EWeLinkAuthError("Нет токена доступа — сначала пройдите авторизацию (authorize_url/exchange_code)")
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"
        else:
            headers["Authorization"] = f"Sign {self._sign(body)}"
        return headers

    def _request(
        self, method: str, path: str, *,
        params: dict | None = None, json_body: dict | None = None, authorized: bool = True,
    ) -> dict:
        url = f"{self._host()}{path}"
        # Подпись (см. _sign) считается для тела, сериализованного компактно
        # (без пробелов после "," и ":"). Если отдать requests.request(json=...),
        # оно сериализует тело заново своими сепараторами (с пробелами) — байты
        # запроса разойдутся с байтами, которые были подписаны, и eWeLink
        # ответит "sign verification failed". Поэтому сериализуем тело сами и
        # передаём готовые байты через data=, используя ту же строку для подписи
        # (см. _headers/_sign) и для отправки.
        body_bytes = (
            json.dumps(json_body, separators=(",", ":")).encode()
            if method != "GET" and json_body is not None
            else None
        )
        try:
            resp = requests.request(
                method, url,
                params=params,
                data=body_bytes,
                headers=self._headers(json_body or {}, authorized=authorized),
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
            # 401/402 — коды невалидного/протухшего токена в открытых
            # клиентах eWeLink; НЕ ПОДТВЕРЖДЕНО живым тестом с этим приложением.
            if error in (401, 402):
                raise EWeLinkAuthError(f"eWeLink: токен недействителен (код {error}): {msg}")
            raise EWeLinkApiError(f"eWeLink error {error}: {msg}")
        return data

    # ---------- авторизация (OAuth2 authorization code) ----------

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        """Строит адрес страницы авторизации eWeLink — председатель кооператива
        переходит по нему в браузере, логинится в приложении eWeLink и даёт
        доступ; eWeLink возвращает браузер на redirect_uri с ?code=&region=&state=.
        state должен быть проверен на стороне callback (см.
        app/electricity_monitor.py:oauth_callback) — защита от CSRF/подмены
        чужого кода авторизации."""
        seq = str(int(time.time() * 1000))
        nonce = secrets.token_hex(4)
        params = {
            "state": state,
            "clientId": self.app_id,
            "authorization": self._authorize_signature(seq),
            "nonce": nonce,
            "seq": seq,
            "redirectUrl": redirect_uri,
            "grantType": "authorization_code",
            "showQRCode": "false",
        }
        return f"{AUTHORIZE_PAGE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> EWeLinkTokens:
        """Меняет code, полученный на redirect_uri после авторизации, на пару
        токенов — POST /v2/user/oauth/token. self.region должен быть уже
        выставлен вызывающим кодом из ?region= в query string callback'а
        (eWeLink обслуживает пользователя не всегда на DEFAULT_REGION)."""
        body = {"code": code, "redirectUrl": redirect_uri, "grantType": "authorization_code"}
        data = self._request("POST", "/v2/user/oauth/token", json_body=body, authorized=False)
        payload = data["data"]
        access_token, refresh_token = _extract_token_pair(payload)
        new_region = payload.get("region")
        if new_region:
            self.region = new_region
        self.tokens = EWeLinkTokens(
            access_token=access_token, refresh_token=refresh_token,
            region=self.region, obtained_at=time.time(),
        )
        return self.tokens

    def refresh(self) -> EWeLinkTokens:
        if not self.tokens:
            raise EWeLinkAuthError("Нет текущих токенов для refresh()")
        body = {"rt": self.tokens.refresh_token}
        data = self._request("POST", "/v2/user/refresh", json_body=body, authorized=False)
        payload = data["data"]
        access_token, refresh_token = _extract_token_pair(payload)
        if refresh_token != self.tokens.refresh_token:
            self.rotated_refresh_token = refresh_token
        self.tokens = EWeLinkTokens(
            access_token=access_token, refresh_token=refresh_token,
            region=self.region, obtained_at=time.time(),
        )
        return self.tokens

    def unbind(self) -> None:
        """Отвязывает это приложение от аккаунта eWeLink (DELETE
        /v2/user/oauth/token) — используется, если председатель хочет
        полностью сбросить подключение, а не просто ввести другие данные."""
        self._request("DELETE", "/v2/user/oauth/token", json_body={}, authorized=True)

    # ---------- дом/устройства ----------

    def list_families(self) -> list[dict]:
        """GET /v2/family — список домов пользователя (у аккаунта минимум один,
        см. официальную документацию). Нужен для выбора family_id, без
        которого не работает list_devices()."""
        data = self._request("GET", "/v2/family", params={"lang": "en"}, authorized=True)
        return data.get("data", {}).get("familyList", [])

    def list_devices(self, family_id: str) -> list[dict]:
        """GET /v2/device/thing — все группы и устройства указанного дома.
        Имя query-параметра family id ("familyid") НЕ ПОДТВЕРЖДЕНО — см.
        предупреждение в начале файла."""
        if not family_id:
            raise EWeLinkApiError("Не выбран дом (family) — сначала выберите его в настройках подключения к eWeLink")
        data = self._request("GET", "/v2/device/thing", params={"lang": "en", "familyid": family_id}, authorized=True)
        return data.get("data", {}).get("thingList", [])

    def get_phase_snapshot(self, ewelink_device_id: str, family_id: str, devices: list[dict] | None = None) -> PhaseSnapshot:
        """devices можно передать заранее полученным списком (list_devices()),
        чтобы не делать отдельный запрос на каждую из 3 фаз — см.
        scripts/poll_ewelink.py, который запрашивает список один раз на
        весь цикл опроса."""
        if devices is None:
            devices = self.list_devices(family_id)
        for d in devices:
            item = d.get("itemData", d)
            if item.get("deviceid") == ewelink_device_id:
                return parse_phase_snapshot(d)
        raise EWeLinkApiError(f"Устройство {ewelink_device_id} не найдено в списке eWeLink")
