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
5. **Подтверждено живым запросом (с заведомо неверным паролем)**: ответ на
   POST — JSON, не HTML: `{"success": bool, "errors": [...], "data": {...}}`,
   Content-Type: application/json, без каких-либо AJAX-заголовков в
   запросе. При `success: false` — понятный текст в errors (напр.
   "Неверный логин или пароль") — именно это подтверждает, что наш
   RSA-шаг РАБОТАЕТ: сервер сумел расшифровать __RSA_DATA и дошёл до
   проверки пароля по существу, а не отверг запрос как испорченный.
6. **Подтверждено живым запросом с НАСТОЯЩИМИ учётными данными**: при
   `success: true` сама числовая величина баланса не приходит ни в этом
   JSON, ни в HTML главной страницы ЛК (`/`) — виджет баланса в разметке
   страницы — это `<div class="asyncLoader" data-js-async-loader="{...
   &quot;url&quot;:&quot;/bitrix/services/main/ajax.php?mode=class&c=
   delement:contract.balance.info&action=getBalance&template=balance&quot;
   ...}">`, т.е. подгружается ОТДЕЛЬНЫМ AJAX-запросом уже после отрисовки
   страницы (стандартный компонент 1С-Битрикс — "async component").
   Достаточно POST на этот URL той же сессией (cookies) — ответ:
   `{"isSuccess": true, "data": {"html": "<article>...<span
   class=\"formattedValue__main\">3 314,09</span>...</article>"}}` —
   тот же формат HTML-фрагмента внутри, что и раньше ожидалось на целой
   странице, просто внутри JSON-обёртки. Число внутри — реальный баланс,
   сверено с тем, что председатель видел глазами в браузере.
7. ЛК показывает долг кооператива ПОЛОЖИТЕЛЬНЫМ числом — обратный знак
   по сравнению с конвенцией остальных провайдеров этого проекта
   (отрицательное = кооператив должен, см. CounterpartyApiProvider) —
   поэтому знак инвертируется перед возвратом.

Вся цепочка — от RSA-шифрования до самого числа баланса — подтверждена
живыми запросами к lk-b2b-yar.tns-e.ru с настоящими учётными данными
кооператива, включая сверку итогового числа с тем, что видно в браузере.
Единственное, что остаётся неподтверждённым: ведёт ли себя так же ЛК
других регионов ТНС-Энерго (не только yar) — общий шаблон Битрикса,
скорее всего, идентичен, но не проверялось.
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
# Async-компонент Битрикса, отдающий HTML-фрагмент с балансом — подтверждено
# живым запросом (см. докстринг модуля, шаг 6), найден по атрибуту
# data-js-async-loader на главной странице ЛК после входа.
_BALANCE_AJAX_PATH = "/bitrix/services/main/ajax.php?mode=class&c=delement:contract.balance.info&action=getBalance&template=balance"


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
            auth_resp = session.post(
                f"{self.base_url}/auth/",
                data={"AUTH_TYPE": "LEGAL", "AUTH_ACTION": "Войти", "USER_LOGIN": self.email, "__RSA_DATA": rsa_data},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CounterpartyApiError(f"не удалось выполнить вход в ТНС-Энерго Бизнес: {exc}") from exc

        # Ответ на попытку входа — JSON (подтверждено живым запросом, см.
        # докстринг модуля), не HTML: {"success": bool, "errors": [...]}.
        # При success: false — понятный текст ошибки уже готов в errors,
        # не нужно гадать по отсутствию виджета баланса.
        try:
            auth_payload = auth_resp.json()
        except ValueError as exc:
            raise CounterpartyApiError(f"ТНС-Энерго Бизнес вернул нераспознаваемый ответ на попытку входа: {exc}") from exc

        if not auth_payload.get("success"):
            errors = auth_payload.get("errors") or []
            message = "; ".join(errors) if errors else "вход отклонён без описания причины"
            raise CounterpartyApiError(f"ТНС-Энерго Бизнес: {message}")

        # Баланс — отдельный async-компонент Битрикса (см. докстринг,
        # шаг 6), не часть страницы входа и не часть обычной HTML-страницы
        # ЛК — сессия (cookies) уже аутентифицирована после успешного POST
        # выше, достаточно дёрнуть этот эндпоинт напрямую.
        try:
            balance_resp = session.post(f"{self.base_url}{_BALANCE_AJAX_PATH}", timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise CounterpartyApiError(f"вход выполнен, но не удалось получить баланс: {exc}") from exc

        try:
            balance_payload = balance_resp.json()
        except ValueError as exc:
            raise CounterpartyApiError(f"ТНС-Энерго Бизнес вернул нераспознаваемый ответ на запрос баланса: {exc}") from exc

        if not balance_payload.get("isSuccess"):
            raise CounterpartyApiError(
                "ТНС-Энерго Бизнес отклонил запрос баланса — возможно, сессия истекла "
                "или логин/пароль/регион всё же неверны"
            )

        fragment_html = (balance_payload.get("data") or {}).get("html") or ""
        amount = _extract_balance(fragment_html)
        if amount is None:
            raise CounterpartyApiError(
                "вход выполнен, но не удалось найти баланс в ответе сервера — "
                "возможно, изменилась вёрстка сайта"
            )

        return BalanceInfo(amount=-amount, as_of=dt.date.today())
