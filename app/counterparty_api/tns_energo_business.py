"""
Клиент баланса личного кабинета для юрлиц ТНС-Энерго Бизнес
(lk-b2b-<регион>.tns-e.ru — у кооператива lk-b2b-yar.tns-e.ru,
Ярославская область). НЕ путать с личным кабинетом для физлиц
(lk.<регион>.tns-e.ru, вход по номеру лицевого счёта, другой протокол —
см. models.CounterpartyApiProvider docstring) — на GitHub есть готовая
библиотека (alryaz/tns-energo-api), но она именно под тот, другой ЛК, и
здесь не подходит.

Это НЕ документированный JSON API, а веб-форма 1С-Битрикс с
client-side RSA-шифрованием пароля (модуль main.rsasecurity) — простой
POST с паролем открытым текстом сервер отклонит. Алгоритм восстановлен
чтением реального JS с lk-b2b-yar.tns-e.ru (не документация — ссылок на
официальное API Битрикса для этого нет):
`/bitrix/js/main/rsasecurity.min.js`, функции `rsasec_form`/
`rsasec_crypt`/`biFromRaw`/`biToRaw`.

Алгоритм:
1. GET /auth/ (с сохранением cookies сессии) — на странице инлайн-скрипт
   вида `rsasec_form_bind({"formid":"form_auth","key":{"M":<base64>,
   "E":<base64>,"chunk":128},"rsa_rand":"<rand>","params":["USER_PASSWORD",
   "USER_CONFIRM_PASSWORD"]})`. M/E — RSA-модуль/экспонента как raw-байты
   в base64; biFromRaw трактует эти байты как LITTLE-ENDIAN (первый байт —
   младший) — не как обычно принято для RSA-модулей (big-endian).
   Модуль/экспонента/rsa_rand свои на каждую загрузку страницы и
   привязаны к сессии — cookies GET обязательно переиспользовать в POST.
2. Собирается строка `__RSA_RAND=<rand>&USER_PASSWORD=<urlencoded
   пароль>&__SHA=<sha1_hex того, что уже собрано>` (USER_CONFIRM_PASSWORD
   пропускается — этого поля нет в форме входа по email).
3. RSA-шифрование БЕЗ padding (учебная схема Битрикса, не PKCS1/OAEP):
   строка дополняется нулевыми байтами до кратности chunk (128 байт = ключ
   1024 бита), каждый блок — little-endian целое, `pow(блок, E, M)`,
   результат — снова little-endian байты МИНИМАЛЬНОЙ длины (без
   выравнивания до chunk), base64 каждого блока, блоки склеиваются
   ПРОБЕЛОМ — это и есть `__RSA_DATA`.
4. POST /auth/ той же сессией: AUTH_TYPE=LEGAL, AUTH_ACTION=Войти,
   USER_LOGIN=<email>, __RSA_DATA=<собранное>. Поле USER_PASSWORD в форме
   реальный браузер НЕ отправляет (JS отключает input перед сабмитом).
5. Баланс виден на любой странице ЛК после входа (подтверждено на живой
   странице) — первый элемент с классом formattedValue__main. Если он не
   найден в ответе — считаем, что вход не удался (неверный логин/пароль/
   регион), а не пытаемся отличить это от прочих ошибок сайта.
6. ЛК показывает долг кооператива ПОЛОЖИТЕЛЬНЫМ числом — обратный знак
   по сравнению с конвенцией остальных провайдеров этого проекта
   (отрицательное = кооператив должен, см. CounterpartyApiProvider) —
   поэтому знак инвертируется перед возвратом.

ЧЕСТНАЯ ОГОВОРКА (как и с API Сбербанка в этом проекте, см. context.md):
сам факт, что эта самодельная реализация RSA-шага будет принята сервером
Битрикса — живым логином не проверялся (нет тестовых учётных данных) и
не может быть проверен без реального аккаунта. Первая реальная проверка —
кнопка «Обновить баланс» после того, как председатель введёт настоящие
логин/пароль/регион на карточке контрагента.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests

from ..bank_api.base import BalanceInfo
from ..i18n import parse_decimal
from .base import CounterpartyApiClient, CounterpartyApiError

REQUEST_TIMEOUT = 15  # секунд — тот же порядок, что и у остальных клиентов проекта

_RSASEC_MARKER = "rsasec_form_bind"
_BALANCE_RE = re.compile(r'formattedValue__main["\']?[^>]*>\s*([^<]+?)\s*<')


def _extract_rsa_params(html: str) -> dict:
    """Достаёт JSON-объект параметров из инлайнового вызова
    rsasec_form_bind({...}) — json.JSONDecoder().raw_decode() сам
    находит конец объекта, без хрупкого regex на парные скобки."""
    idx = html.find(_RSASEC_MARKER)
    if idx == -1:
        raise CounterpartyApiError(
            "на странице входа ТНС-Энерго Бизнес не найден блок параметров авторизации "
            "(rsasec_form_bind) — возможно, сайт изменился, или неверный регион"
        )
    start = html.find("{", idx)
    if start == -1:
        raise CounterpartyApiError("не удалось разобрать параметры входа ТНС-Энерго Бизнес")
    try:
        params, _ = json.JSONDecoder().raw_decode(html, start)
    except ValueError as exc:
        raise CounterpartyApiError(f"не удалось разобрать параметры входа ТНС-Энерго Бизнес: {exc}") from exc
    return params


def _build_rsa_plaintext(rsa_rand: str, password: str, param_names: list) -> str:
    """Строка для RSA-шифрования — см. шаг 2 в докстринге модуля. Только
    USER_PASSWORD реально присутствует в форме входа по email; остальные
    имена из param_names (например USER_CONFIRM_PASSWORD) — молча
    пропускаются, как это делает и сам rsasec_form() в браузере (у
    несуществующего в форме поля element будет null)."""
    values = {"USER_PASSWORD": password}
    parts = [f"__RSA_RAND={rsa_rand}"]
    for name in param_names:
        if name in values:
            parts.append(f"{name}={quote(values[name], safe='')}")
    plaintext = "&".join(parts)
    plaintext += "&__SHA=" + hashlib.sha1(plaintext.encode("utf-8")).hexdigest()
    return plaintext


def _rsa_encrypt(plaintext: str, e: int, m: int, chunk_size: int) -> str:
    """Реализация rsasec_crypt() — см. докстринг модуля, шаг 3. Каждый
    блок читается/пишется как little-endian целое (не общепринятый для
    RSA big-endian) — так исторически сделано в исходном JS
    (biFromRaw/biToRaw), и сервер расшифровывает ровно так же."""
    data = plaintext.encode("utf-8")
    pad = (-len(data)) % chunk_size
    data += b"\x00" * pad

    chunks = []
    for i in range(0, len(data), chunk_size):
        block = data[i:i + chunk_size]
        value = int.from_bytes(block, "little")
        cipher = pow(value, e, m)
        ndigits = max(1, (cipher.bit_length() + 15) // 16)  # 16-битные "цифры" biToRaw
        raw = cipher.to_bytes(ndigits * 2, "little")
        chunks.append(base64.b64encode(raw).decode("ascii"))
    return " ".join(chunks)


def _extract_balance(html: str) -> Decimal | None:
    """Первый элемент с классом formattedValue__main на странице —
    подтверждено, что баланс виден на любой странице ЛК после входа, так
    что достаточно первого найденного (не нужно искать конкретный раздел
    «Лицевой счёт»)."""
    match = _BALANCE_RE.search(html)
    if not match:
        return None
    try:
        return parse_decimal(match.group(1))
    except InvalidOperation:
        return None


class TnsEnergoBusinessBalanceClient(CounterpartyApiClient):
    def __init__(self, email: str, password: str, region: str):
        self.email = email
        self.password = password
        self.region = region
        self.base_url = f"https://lk-b2b-{region}.tns-e.ru"

    def get_balance(self) -> BalanceInfo:
        session = requests.Session()

        try:
            login_page = session.get(f"{self.base_url}/auth/", timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise CounterpartyApiError(f"не удалось открыть страницу входа ТНС-Энерго Бизнес: {exc}") from exc

        params = _extract_rsa_params(login_page.text)
        try:
            key = params["key"]
            modulus = int.from_bytes(base64.b64decode(key["M"]), "little")
            exponent = int.from_bytes(base64.b64decode(key["E"]), "little")
            chunk_size = int(key["chunk"])
            plaintext = _build_rsa_plaintext(params["rsa_rand"], self.password, params.get("params", ["USER_PASSWORD"]))
        except (KeyError, ValueError) as exc:
            raise CounterpartyApiError(f"не удалось разобрать параметры входа ТНС-Энерго Бизнес: {exc}") from exc

        rsa_data = _rsa_encrypt(plaintext, exponent, modulus, chunk_size)

        try:
            result_page = session.post(
                f"{self.base_url}/auth/",
                data={"AUTH_TYPE": "LEGAL", "AUTH_ACTION": "Войти", "USER_LOGIN": self.email, "__RSA_DATA": rsa_data},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CounterpartyApiError(f"не удалось выполнить вход в ТНС-Энерго Бизнес: {exc}") from exc

        amount = _extract_balance(result_page.text)
        if amount is None:
            raise CounterpartyApiError(
                "не удалось найти баланс после входа в ТНС-Энерго Бизнес — "
                "проверьте логин, пароль и регион (поддомен) личного кабинета"
            )

        return BalanceInfo(amount=-amount, as_of=dt.date.today())
