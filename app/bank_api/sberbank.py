"""
Клиент API СберБизнес (Sber API / бывший SberBusiness API, «Fintech API») —
прямая интеграция для получения баланса, выписки, и работы с реестром
начислений/реестром платежей.

**Авторизация — authorization_code + refresh_token, НЕ client_credentials.**
Первая версия этого модуля делала OAuth2 client_credentials — это было
неверно: по официальной документации Sber API (developers.sber.ru/docs/ru/
sber-api/start/oauth) все запросы выполняются от имени конкретного
пользователя СберБизнес; авторизация выдаётся через access_token +
refresh_token, которые проще всего получить сразу готовыми — в Личном
кабинете Sber API (developers.sber.ru → ваш сервис → «Ключи доступа» →
сгенерировать access_token/refresh_token) — председатель вводит оба в
интерфейсе кооператива один раз, дальше приложение само обновляет
access_token через refresh_token перед каждым обращением к банку (см.
_get_access_token ниже). access_token живёт 60 минут, поэтому в БД не
хранится вообще — обновляется заново при каждой синхронизации;
refresh_token живёт 180 дней и МОЖЕТ РОТИРОВАТЬСЯ (банк при обновлении
токена иногда выдаёт новый refresh_token взамен старого) — это
обрабатывается через self.rotated_refresh_token (см. ниже и
app/bank_sync.py: вызывающий код обязан проверить этот атрибут после
работы с клиентом и пересохранить обновлённый refresh_token).

Адреса ресурсов ниже (statement/transactions, statement/summary,
debt-registries, payments-registry) взяты из официальной документации
(developers.sber.ru/docs/ru/sber-api/...), но **ни разу не проверялись
живым запросом за пределами получения access_token** — первым делом при
ошибках сверяться со Swagger в личном кабинете организации на
developers.sber.ru.

**Российская специфика TLS — без неё ничего не заработает, даже с
правильными токенами:**
1. Сервер банка предъявляет TLS-сертификат, выпущенный удостоверяющими
   центрами Сбера И Национальным удостоверяющим центром Минцифры России
   (НУЦ Минцифры) — официально подтверждено (developers.sber.ru/docs/ru/
   sber-api/start/tls: «добавить в доверенные корневые сертификаты
   удостоверяющих центров Сбера и Минцифры России») — обычный набор
   доверенных корневых сертификатов (тот, что использует requests/certifi
   по умолчанию) их не знает. Нужен CA bundle с обеими цепочками — банк
   отдаёт готовый архив по прямой ссылке для тестового
   (https://cdn-app.sberdevices.ru/misc/0.0.0/assets/bsm-docs/b89853b1_chain_test.zip)
   и промышленного
   (https://cdn-app.sberdevices.ru/misc/0.0.0/assets/bsm-docs/f8dd5e00_chain_prom.zip)
   контуров — надёжнее, чем собирать общий сертификат Минцифры с госуслуг
   отдельно. Путь к получившемуся файлу — Config.SBERBANK_API_CA_BUNDLE
   (см. .env.example).
2. Банк отдельно требует клиентский mTLS-сертификат для самого
   соединения — выдаётся в личном кабинете Sber API в формате PKCS#12
   (.pfx/.p12) с паролем, действует 12 месяцев и только для ОДНОГО
   client_id, для которого выпущен. При генерации через личный кабинет
   .p12-контейнер уже включает цепочку сертификатов (не только клиентский
   сертификат) — app/bank_sync.py: _save_client_cert сохраняет её всю, не
   только лист. Конвертируется в PEM при загрузке в интерфейсе кооператива
   и хранится в BANK_CERTS_FOLDER — каталоге вне обычных /uploads, не
   отдаваемом ни одним HTTP-роутом.
3. Отдельно от mTLS-соединения — ГОСТ-электронная подпись (УНЭП/УКЭП) для
   автоматического подписания платёжных поручений без подтверждения в
   СберБизнес. Эта интеграция такое не делает (только чтение баланса/
   выписки/статуса реестров и отправка данных реестра начислений — не
   платёжные поручения), поэтому ЭП здесь сознательно не реализована; если
   в будущем понадобится инициировать платежи через API — потребуется
   отдельная инфраструктура (СКЗИ, токен/рутокен, профиль подписанта).

**Реестр начислений/реестр платежей — отдельный канал, не RKO, и, судя по
реальному списку выданных операций (scope) в личном кабинете, возможно,
вообще НЕ входит в состав этого продукта.** В отличие от баланса и
выписки (современный Fintech API, JSON, подтверждённые операции
GET_STATEMENT_ACCOUNT/GET_STATEMENT_TRANSACTION в scope), приём начислений
от физлиц и выдача реестра платежей по ним — более старый функционал
СберБизнес Онлайн («Система приёма платежей»), исторически работающий
текстовыми файлами в кодировке **Windows-1251**, а не JSON (см.
app/bank_api/registry_file.py). Официальный перечень операций (developers.
sber.ru/docs/ru/sber-api/start/oauth#polnyy-perechen-operatsiy) содержит
`PAYMENTS_REGISTRY` («Реестр платежей») как отдельную операцию scope —
если её нет в выданном вам наборе прав, `get_payment_registry`/
`send_charge_registry` ниже будут получать отказ доступа независимо от
корректности токена/сертификатов; для начислений (`DEBT_REGISTRY`,
«приём платежей физлиц») подходящей операции в перечне не нашлось вообще
— возможно, это отдельный, не входящий в общий Sber API продукт (тогда
единственный рабочий путь — не автоматический через это API, а ручной
файлообмен через веб-интерфейс СберБизнес Онлайн, см. app/bank_sync.py:
download_charge_registry_file/upload_payment_registry_file — эти два пути
работают независимо от API и всегда доступны).
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import requests

from .base import (
    BankApiClient, BankApiError, BalanceInfo, StatementLine,
    ChargeRegistryItem, ChargeRegistryResult, PaymentRegistryItem,
)
from . import registry_file

# Тестовый контур подтверждён напрямую в личном кабинете Sber API
# (fintech-test.sberbank.ru:9443). Промышленный — по аналогии из открытой
# документации (developers.sber.ru/docs/ru/sber-api/...), не проверялся
# живым запросом — если он окажется другим, переопределяется тем же путём,
# что и TOKEN_URL, через SBERBANK_API_BASE_URL (см. .env.example).
SANDBOX_BASE_URL = "https://fintech-test.sberbank.ru:9443"
PROD_BASE_URL = "https://fintech.sberbank.ru:9443"

# Путь получения/обновления токена — относительный путь /ic/sso/api/v2/oauth/token
# подтверждён документацией (developers.sber.ru/docs/ru/sber-api/start/oauth),
# хост — тот же, что и у самого API (base_url), это стандартная практика
# для таких интеграций и то, что подошло бы по структуре личного кабинета;
# если у вашего подключения хост авторизации отличается от хоста API —
# переопределяется через SBERBANK_API_TOKEN_URL (см. .env.example), тогда
# он не будет пересчитываться от base_url.
DEFAULT_TOKEN_PATH = "/ic/sso/api/v2/oauth/token"

REQUEST_TIMEOUT = 30  # секунд


class SberbankClient(BankApiClient):
    def __init__(
        self, client_id: str, client_secret: str, refresh_token: str, account_number: str,
        sandbox: bool = True, base_url: str | None = None, token_url: str | None = None,
        client_cert: tuple[str, str] | None = None, ca_bundle: str | None = None,
        registry_format: "registry_file.RegistryFormat | None" = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        # Если банк при обновлении токена вернул НОВЫЙ refresh_token —
        # сюда попадает актуальное значение; вызывающий код (app/bank_sync.py)
        # обязан проверить этот атрибут после использования клиента и
        # пересохранить его вместо старого — иначе следующее обновление
        # access_token не пройдёт (старый refresh_token банк аннулирует
        # при ротации). None, пока обновления токена не происходило или
        # банк не поменял refresh_token.
        self.rotated_refresh_token: str | None = None
        self.account_number = account_number
        self.base_url = base_url or (SANDBOX_BASE_URL if sandbox else PROD_BASE_URL)
        self.token_url = token_url or f"{self.base_url}{DEFAULT_TOKEN_PATH}"
        self._access_token: str | None = None
        # mTLS: банк требует клиентский сертификат для самого TLS-соединения
        # (не только токен в заголовке) — см. models.py:
        # BankApiCredential.tls_cert_filename/tls_key_filename. Без него
        # ЛЮБОЙ вызов ниже упадёт на этапе установления соединения, ещё до
        # авторизации. ca_bundle — доверенные корни банка (Сбер + НУЦ
        # Минцифры, см. config.py: SBERBANK_API_CA_BUNDLE) для проверки
        # серверного сертификата; если не передан, используется обычный
        # системный набор доверенных корней (requests/certifi по
        # умолчанию), который сертификаты банка не знает — соединение
        # завершится SSLError.
        self.client_cert = client_cert
        self.ca_bundle = ca_bundle or True
        # Формат CP1251-файла реестров — настраиваемый по счёту (см.
        # models.BankRegistryFormat), т.к. зависит от конкретного договора;
        # если не передан явно — берётся реальный формат из образцов файлов
        # (registry_file.DEFAULT_FORMAT), не выдуманный.
        self.registry_format = registry_format or registry_file.DEFAULT_FORMAT

    # -- авторизация ---------------------------------------------------

    def _get_access_token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        try:
            resp = requests.post(
                self.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                cert=self.client_cert, verify=self.ca_bundle,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            token = payload.get("access_token")
        except requests.exceptions.SSLError as e:
            raise BankApiError(
                "Ошибка проверки TLS-сертификата банка (СберБизнес использует сертификаты УЦ Сбера и "
                "НУЦ Минцифры) или клиентского mTLS-сертификата — проверьте настройки SBERBANK_API_CA_BUNDLE "
                f"и сертификат счёта: {e}"
            ) from e
        except requests.RequestException as e:
            raise BankApiError(
                f"Не удалось обновить access_token СберБизнес по refresh_token: {e}"
            ) from e
        if not token:
            raise BankApiError(
                "Ответ обновления токена СберБизнес не содержит access_token — refresh_token мог "
                "истечь (действует 180 дней) или быть отозван, получите новый в личном кабинете Sber API."
            )
        # Ротация refresh_token — банк может (не обязан) выдать новый взамен
        # использованного; если выдал, старый token дальше не сработает.
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.rotated_refresh_token = new_refresh
            self.refresh_token = new_refresh
        self._access_token = token
        return token

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._get_access_token()}",
        }

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = requests.get(
                f"{self.base_url}{path}", headers=self._headers(), params=params,
                cert=self.client_cert, verify=self.ca_bundle, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError as e:
            raise BankApiError(f"Ошибка TLS-соединения с банком ({path}): {e}") from e
        except requests.RequestException as e:
            raise BankApiError(f"Ошибка запроса к СберБизнес ({path}): {e}") from e

    def _post(self, path: str, json_body: dict) -> dict:
        try:
            resp = requests.post(
                f"{self.base_url}{path}", headers=self._headers(), json=json_body,
                cert=self.client_cert, verify=self.ca_bundle, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError as e:
            raise BankApiError(f"Ошибка TLS-соединения с банком ({path}): {e}") from e
        except requests.RequestException as e:
            raise BankApiError(f"Ошибка запроса к СберБизнес ({path}): {e}") from e

    def _post_file(self, path: str, filename: str, content: bytes, params: dict | None = None) -> bytes:
        """Для реестра начислений/платежей (см. модуль registry_file) — банк
        принимает CP1251-текстовый файл, не JSON. Возвращает «сырое» тело
        ответа (может быть JSON-квитанцией о приёме или пустым 200) —
        разбор оставлен вызывающему методу, здесь только транспорт."""
        try:
            resp = requests.post(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self._get_access_token()}"},  # без Accept: application/json — тело не JSON
                files={"file": (filename, content, "text/plain; charset=windows-1251")},
                params=params or {},
                cert=self.client_cert, verify=self.ca_bundle, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.SSLError as e:
            raise BankApiError(f"Ошибка TLS-соединения с банком ({path}): {e}") from e
        except requests.RequestException as e:
            raise BankApiError(f"Ошибка отправки файла реестра в СберБизнес ({path}): {e}") from e

    def _get_file(self, path: str, params: dict) -> bytes:
        try:
            resp = requests.get(
                f"{self.base_url}{path}", headers={"Authorization": f"Bearer {self._get_access_token()}"},
                params=params, cert=self.client_cert, verify=self.ca_bundle, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.SSLError as e:
            raise BankApiError(f"Ошибка TLS-соединения с банком ({path}): {e}") from e
        except requests.RequestException as e:
            raise BankApiError(f"Ошибка запроса файла реестра к СберБизнес ({path}): {e}") from e

    # -- баланс и выписка ------------------------------------------------
    # См. developers.sber.ru/docs/ru/sber-api/host/transactions

    def get_balance(self) -> BalanceInfo:
        today = dt.date.today()
        data = self._get(
            "/fintech/api/v2/statement/summary",
            {"accountNumber": self.account_number.replace(" ", "").replace("-", ""), "statementDate": today.isoformat()},
        )
        closing = data.get("closingBalanceRub") or data.get("closingBalance") or {}
        amount = closing.get("amount")
        if amount is None:
            raise BankApiError("Ответ /statement/summary не содержит closingBalance.")
        return BalanceInfo(amount=Decimal(str(amount)), as_of=today)

    def get_statement(self, date_from: dt.date, date_to: dt.date) -> list[StatementLine]:
        """Fintech API отдаёт выписку постранично ЗА ОДНУ ДАТУ (statementDate), не за
        диапазон — поэтому здесь цикл по дням и по страницам внутри каждого дня."""
        lines: list[StatementLine] = []
        day = date_from
        while day <= date_to:
            page = 1
            while True:
                data = self._get(
                    "/fintech/api/v2/statement/transactions",
                    {"accountNumber": self.account_number.replace(" ", "").replace("-", ""), "statementDate": day.isoformat(), "page": page},
                )
                transactions = data.get("transactions") or []
                if not transactions:
                    break
                for t in transactions:
                    lines.append(_parse_transaction(t, day))
                if len(transactions) < 100:  # меньше полной страницы — дальше страниц для этого дня нет
                    break
                page += 1
            day += dt.timedelta(days=1)
        return lines

    # -- реестр начислений ------------------------------------------------
    # Приём начислений от физлиц для показа/оплаты в Сбербанк Онлайн —
    # отдельный, более старый канал СберБизнес Онлайн, работающий CP1251-
    # текстовым файлом, а не JSON (см. модуль registry_file и пояснение в
    # начале этого файла). Путь ресурса `/fintech/api/v1/debt-registries`
    # взят по аналогии с современными Fintech-эндпоинтами RKO (по смыслу —
    # тот же механизм, что бизнес называет «реестром начислений»,
    # документация называет «реестром задолженности»), но именно для файловой
    # передачи он НЕ подтверждён — не исключено, что для вашего подключения
    # этот путь другой или загрузка вообще доступна только вручную через
    # веб-интерфейс СберБизнес Онлайн (тогда используйте
    # app/bank_sync.py: download_charge_registry_file — тот же CP1251-файл,
    # но без автоматической отправки).

    def send_charge_registry(self, items: list[ChargeRegistryItem], period: str) -> ChargeRegistryResult:
        content = registry_file.build_charge_registry_file(items, self.registry_format)
        filename = f"charges_{period.replace(' ', '_')}.txt"
        raw = self._post_file("/fintech/api/v1/debt-registries", filename, content, params={"period": period})
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        external_id = data.get("externalId") or data.get("id")
        if not external_id:
            raise BankApiError(
                "Банк не вернул externalId для отправленного реестра начислений "
                "(либо ответ пришёл не в ожидаемом формате — сверьте со спецификацией банка)."
            )
        return ChargeRegistryResult(
            external_id=external_id, status=data.get("bankStatus", "sent"), bank_comment=data.get("bankComment"),
        )

    def get_charge_registry_status(self, external_id: str) -> ChargeRegistryResult:
        data = self._get(f"/fintech/api/v1/debt-registries/{external_id}/state", {})
        return ChargeRegistryResult(
            external_id=external_id,
            status=data.get("bankStatus", "unknown"),
            bank_comment=data.get("bankComment"),
        )

    # -- реестр платежей ---------------------------------------------------
    # Реестр платежей — только ручной путь: скачать файл через
    # СберБизнес Онлайн и загрузить в интерфейсе кооператива (см.
    # app/bank_sync.py: upload_payment_registry_file).
    # REST API для этого канала недоступен.


def _parse_transaction(t: dict, fallback_date: dt.date) -> StatementLine:
    amount_obj = t.get("amountRub") or t.get("amount") or {}
    direction_raw = (t.get("direction") or "").upper()
    direction = "credit" if direction_raw == "CREDIT" else "debit"
    counterparty = t.get("rurTransfer") or {}
    is_credit = direction == "credit"
    return StatementLine(
        external_uid=t.get("uuid"),
        operation_date=_parse_date(t.get("operationDate")) or fallback_date,
        direction=direction,
        amount=Decimal(str(amount_obj.get("amount", "0"))),
        counterparty_name=counterparty.get("payerName") if is_credit else counterparty.get("receiverName"),
        counterparty_inn=counterparty.get("payerInn") if is_credit else counterparty.get("receiverInn"),
        payment_purpose=t.get("paymentPurpose"),
        document_number=t.get("number"),
    )


def _parse_date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None
