"""
Клиент API Модульбанка (https://api.modulbank.ru/) — баланс и выписка
(история операций) расчётного счёта.

Всё ниже сверено со спецификацией банка
(https://api.modulbank.ru/v1/swagger/docs/v1, ноябрь 2026), но **живым
запросом к промышленному контуру не проверялось**: при первой ошибке
сверяться с ней же.

**Авторизация — один токен, без refresh_token и без mTLS.** Токен
генерируется в личном кабинете Модульбанка (меню «+» → «Интеграция с
банком» / «Подключиться к API») и передаётся как `Authorization: Bearer
<токен>`. Токен привязан к учётной записи пользователя ЛК, хранится
зашифрованным (BankApiCredential.api_token_encrypted). OAuth-вариант
(clientId/clientSecret от банка по письму) кооперативу не нужен: он для
приложений, которые подключают чужих клиентов банка.

**TLS-сертификат api.modulbank.ru выдан НУЦ Минцифры** (Russian Trusted
Sub CA → Russian Trusted Root CA), а не публичным УЦ: в certifi, которым
по умолчанию проверяет requests, этого корня нет («self-signed certificate
in certificate chain»). Поэтому проверяем по корню Минцифры из
Config.MODULBANK_API_CA_BUNDLE — по умолчанию
instance/bundles/russian_trusted_root_ca.pem, рядом с bundle.pem Сбера.
В git файла нет: на сервер его кладёт deployment/deploy.sh
(deployment/deploy/README.md, «Сертификаты»). Клиентский сертификат (mTLS), как у Сбера, не нужен.

**Песочница**: заголовок `sandbox: on` и токен `sandboxtoken`. Данные там
тестовые, номера счетов не совпадут с нашими, поэтому в песочнице, если
счёт с нашим номером не найден, берётся первый рублёвый счёт (см.
_resolve_account_id). В песочнице у истории операций работают только
фильтры records/skip, поэтому даты дополнительно фильтруются на нашей
стороне.

**Направление операции — наоборот по сравнению с нашим.** У Модульбанка
`category: "Debet"` — ВХОДЯЩАЯ операция (зачисление), `"Credit"` —
ИСХОДЯЩАЯ. В StatementLine.direction это "credit" и "debit"
соответственно.

**Сумма** (`amount`) — «сумма платежа без учёта банковской комиссии»,
то есть в выписке видна полная оплаченная сумма. При разнесении на
электричество процент счёта вычитается в bank_sync
(accounting.amount_without_bank_fee), как и задумано для банков, которые
списывают комиссию отдельно.

**Веб-хуки** (уведомления о новых операциях) принимает
app/bank_webhooks.py; подпись — webhook_signature() ниже.

**Реестров начислений/платежей в API нет.** Ручной файловый путь
(app/bank_api/registry_file.py) от банка не зависит.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

import os

import requests

from .base import BankApiClient, BankApiError, BalanceInfo, StatementLine

DEFAULT_BASE_URL = "https://api.modulbank.ru/v1"
SANDBOX_TOKEN = "sandboxtoken"

REQUEST_TIMEOUT = 30  # секунд

# Лимит записей на страницу истории операций — «действует лимит компании
# (обычно 50)». Больше не запрашиваем: при превышении банк может отрезать
# страницу молча, и цикл ниже решил бы, что это последняя.
PAGE_SIZE = 50
# Защита от бесконечного цикла, если банк вдруг начнёт отдавать одну и ту же
# полную страницу: 200 страниц × 50 = 10 000 операций — у кооператива столько
# за окно синхронизации не бывает.
MAX_PAGES = 200

# Статусы, которые означают, что деньги реально прошли по счёту. Черновики,
# платежи на подписи/в обработке, отказы и отмены в выписку не попадают —
# иначе отменённое списание висело бы в выписке как настоящее.
EXECUTED_STATUSES = {"Executed", "Received", "PayReceived"}


class ModulbankClient(BankApiClient):
    def __init__(self, token: str, account_number: str, sandbox: bool = False, base_url: str | None = None,
                 ca_bundle: str | None = None):
        self.sandbox = sandbox
        # В песочнице банк принимает только свой тестовый токен — реальный
        # там использовать запрещено, поэтому подменяем, а не требуем от
        # председателя вводить «sandboxtoken» вручную.
        self.token = SANDBOX_TOKEN if sandbox else token
        self.account_number = _normalize_number(account_number)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._account_id: str | None = None
        # None — проверка по certifi (requests по умолчанию); get_client
        # всегда передаёт Config.MODULBANK_API_CA_BUNDLE.
        self.ca_bundle = ca_bundle

    # -- HTTP ------------------------------------------------------------

    def _post(self, path: str, json_body: dict | None = None):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if self.sandbox:
            headers["sandbox"] = "on"
        if self.ca_bundle and not os.path.isfile(self.ca_bundle):
            raise BankApiError(
                f"Нет файла корневого сертификата НУЦ Минцифры {self.ca_bundle} — без него соединение с API "
                "Модульбанка не проверить. На сервер его кладёт deployment/deploy.sh из локального "
                "deployment/deploy/bundles/ (см. deployment/deploy/README.md, «Сертификаты»)."
            )
        try:
            resp = requests.post(
                f"{self.base_url}{path}", headers=headers, json=json_body if json_body is not None else {},
                timeout=REQUEST_TIMEOUT, verify=self.ca_bundle or True,
            )
        except requests.exceptions.SSLError as e:
            raise BankApiError(
                f"Не удалось проверить TLS-сертификат API Модульбанка ({path}): {e}. Сертификат банка выдан "
                f"НУЦ Минцифры; проверка идёт по корню из файла {self.ca_bundle}. Если банк сменил УЦ, "
                "укажите подходящий файл в переменной окружения MODULBANK_API_CA_BUNDLE."
            ) from e
        except requests.RequestException as e:
            raise BankApiError(f"Нет связи с API Модульбанка ({path}): {e}") from e

        if resp.status_code == 401:
            raise BankApiError(
                "Модульбанк не принимает сохранённый токен (401): он отозван, истёк или введён с ошибкой. "
                "Сгенерируйте новый в личном кабинете Модульбанка («Интеграция с банком») и вставьте его "
                "в поле «Токен API»: «Расчётные счета» → «Действия» → «Настроить API»."
            )
        if resp.status_code == 503:
            raise BankApiError("API Модульбанка временно недоступно (503, сервисные работы банка). Повторите позже.")
        if resp.status_code >= 400:
            raise BankApiError(f"Ошибка запроса к Модульбанку ({path}): HTTP {resp.status_code} {_error_text(resp)}")
        try:
            return resp.json()
        except ValueError as e:
            raise BankApiError(f"Модульбанк вернул не JSON ({path}): {resp.text[:300]}") from e

    # -- счёт --------------------------------------------------------------

    def _resolve_account_id(self) -> str:
        """Методы баланса и истории операций принимают не номер счёта, а его
        системный id (uuid) — находим его по номеру в /account-info."""
        if self._account_id is not None:
            return self._account_id
        companies = self._post("/account-info")
        if not isinstance(companies, list):
            raise BankApiError(f"Неожиданный ответ /account-info: {str(companies)[:300]}")
        accounts = [
            acc for company in companies if isinstance(company, dict)
            for acc in (company.get("bankAccounts") or []) if isinstance(acc, dict)
        ]
        for acc in accounts:
            if _normalize_number(acc.get("number")) == self.account_number and acc.get("id"):
                self._account_id = acc["id"]
                return self._account_id
        if self.sandbox:
            for acc in accounts:
                if acc.get("id") and (acc.get("currency") or "RUR") == "RUR":
                    self._account_id = acc["id"]
                    return self._account_id
        known = ", ".join(_normalize_number(a.get("number")) or "?" for a in accounts) or "нет ни одного"
        raise BankApiError(
            f"Счёт {self.account_number} не найден среди счетов, доступных по этому токену ({known}). "
            "Проверьте номер расчётного счёта или «Номер счёта для запросов к банку» в настройках API."
        )

    # -- баланс и выписка ------------------------------------------------

    def get_balance(self) -> BalanceInfo:
        data = self._post(f"/account-info/balance/{self._resolve_account_id()}")
        # Ответ — голое число; на случай обёртки — BalanceContract {"Balance": ...}.
        if isinstance(data, dict):
            data = data.get("Balance", data.get("balance"))
        if data is None or isinstance(data, bool):
            raise BankApiError(f"Ответ баланса Модульбанка без суммы: {str(data)[:300]}")
        try:
            amount = Decimal(str(data))
        except ArithmeticError as e:
            raise BankApiError(f"Не удалось разобрать баланс Модульбанка: {data!r}") from e
        return BalanceInfo(amount=amount, as_of=dt.date.today())

    def get_statement(self, date_from: dt.date, date_to: dt.date) -> list[StatementLine]:
        """История операций за период, постранично по skip/records. Банк
        сортирует по дате проведения по убыванию; пустая или неполная
        страница — последняя."""
        account_id = self._resolve_account_id()
        lines: list[StatementLine] = []
        seen: set[str] = set()
        skip = 0
        for _page in range(MAX_PAGES):
            operations = self._post(
                f"/operation-history/{account_id}",
                {
                    "from": date_from.isoformat(),
                    # till — date-time: голая дата читается как полночь и
                    # отрезала бы операции последнего дня; берём следующий
                    # день, лишнее отсекается фильтром дат ниже.
                    "till": (date_to + dt.timedelta(days=1)).isoformat(),
                    "skip": skip,
                    "records": PAGE_SIZE,
                },
            )
            if not isinstance(operations, list):
                raise BankApiError(f"Неожиданный ответ истории операций Модульбанка: {str(operations)[:300]}")
            for op in operations:
                try:
                    line = _parse_operation(op)
                except (ValueError, ArithmeticError, KeyError, TypeError, AttributeError) as e:
                    raise BankApiError(
                        f"Не удалось разобрать операцию Модульбанка "
                        f"(id={op.get('id') if isinstance(op, dict) else None}): {e!r}; "
                        f"данные операции: {str(op)[:500]}"
                    ) from e
                if line is None or not (date_from <= line.operation_date <= date_to):
                    continue
                # Банк предупреждает: при изменениях между запросами страницы
                # сдвигаются и возможны повторы — дубль по id отбрасываем здесь
                # (в БД дедуп по external_uid тоже есть, но в пределах одной
                # выгрузки повтор дал бы нарушение уникальности).
                if line.external_uid in seen:
                    continue
                seen.add(line.external_uid)
                lines.append(line)
            if len(operations) < PAGE_SIZE:
                return lines
            skip += PAGE_SIZE
        raise BankApiError(
            f"История операций Модульбанка не закончилась за {MAX_PAGES} страниц — выгрузка прервана, "
            "чтобы не зациклиться. Уменьшите период."
        )


def _parse_operation(op: dict) -> StatementLine | None:
    """None — операция, по которой деньги не прошли (черновик, отказ, отмена…)."""
    if op.get("status") not in EXECUTED_STATUSES:
        return None
    if (op.get("currency") or "RUR") != "RUR":
        return None
    op_id = op.get("id")
    if not op_id:
        raise ValueError("в операции нет id")
    category = op.get("category")
    if category == "Debet":
        direction = "credit"  # у Модульбанка Debet — входящая
    elif category == "Credit":
        direction = "debit"   # Credit — исходящая
    else:
        raise ValueError(f"неизвестное направление операции category={category!r}")
    amount = op.get("amount")
    if amount is None or amount == "" or isinstance(amount, bool):
        raise ValueError("в операции нет суммы (amount)")
    operation_date = _parse_date(op.get("executed")) or _parse_date(op.get("created"))
    if operation_date is None:
        raise ValueError("в операции нет даты (executed/created)")
    return StatementLine(
        external_uid=str(op_id),
        operation_date=operation_date,
        direction=direction,
        amount=Decimal(str(amount)),
        counterparty_name=op.get("contragentName") or None,
        counterparty_inn=op.get("contragentInn") or None,
        payment_purpose=op.get("paymentPurpose") or None,
        document_number=op.get("docNumber") or None,
        counterparty_account=_normalize_number(op.get("contragentBankAccountNumber")) or None,
    )


def webhook_signature(token: str, operation_id: str) -> str:
    """Подпись уведомления (веб-хука), как её считает банк для токена из ЛК:
    SHA-1 в HEX от «<первые 10 символов токена>&<id операции>» в UTF-8.
    Подписан только id операции — остальные поля уведомления (сумма,
    назначение…) подписью не защищены, поэтому app/bank_webhooks.py берёт
    их не из уведомления, а заново из истории операций."""
    return hashlib.sha1(f"{token[:10]}&{operation_id}".encode("utf-8")).hexdigest()


def _normalize_number(raw) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _parse_date(raw) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _error_text(resp) -> str:
    """Банк описывает ошибки как {exceptionType, message} — показываем их,
    иначе начало тела ответа."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(payload, dict):
        parts = [str(payload[k]) for k in ("exceptionType", "message", "error") if payload.get(k)]
        if parts:
            return ": ".join(parts)
    return str(payload)[:300]
