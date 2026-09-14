"""
Фабрика клиента API контрагента по его карточке — единственное место,
которое знает, какие провайдеры реально поддержаны (тот же приём, что
app/bank_api/__init__.py для банков и app/sms/__init__.py для СМС).
app/counterparty_sync.py вызывает только get_client() и не импортирует
конкретные клиенты напрямую.

Все поддержанные провайдеры читают креды одинаково — из
Counterparty.api_credential (CounterpartyApiCredential: login/
secret_encrypted/extra, см. app/models.py) — включая SMS Aero: раньше он
был особым случаем и переиспользовал отдельный singleton SmsSettings (тот
же email/api_key, что и для отправки СМС-кодов входа), но по прямой
просьбе креды перенесены сюда же, чтобы у всех контрагентов с API была
одна и та же точка настройки на их карточке. SmsSettings теперь хранит
только ссылку (counterparty_id) на то, какой контрагент обслуживает
отправку — см. app/sms/__init__.py.

Добавление нового провайдера: реализовать CounterpartyApiClient в
отдельном модуле рядом с smsaero.py/beget.py/tns_energo_business.py и
добавить сюда одну строку в SUPPORTED_PROVIDERS + ветку в get_client().
Само поле Counterparty.api_provider уже позволяет выбрать любое значение
enum'а — get_client() для не реализованных провайдеров вернёт None, а
app/counterparty_sync.py покажет пользователю понятное сообщение об этом,
а не упадёт.

ТНС-Энерго Бизнес — единственный провайдер, которому вдобавок к
login/secret нужен ещё и cred.extra (код региона поддомена личного
кабинета, напр. "yar") — без него тоже None (не настроено), тем же
принципом, что и отсутствие login/secret у любого другого провайдера.
"""
from __future__ import annotations

from ..models import Counterparty, CounterpartyApiProvider
from ..bank_api import crypto
from ..i18n import translate as _
from .base import CounterpartyApiClient
from .smsaero import SmsAeroBalanceClient
from .beget import BegetBalanceClient
from .tns_energo_business import TnsEnergoBusinessBalanceClient

SUPPORTED_PROVIDERS = {
    CounterpartyApiProvider.SMSAERO, CounterpartyApiProvider.BEGET, CounterpartyApiProvider.TNS_ENERGO_BUSINESS,
}


def get_client(counterparty: Counterparty) -> CounterpartyApiClient | None:
    """None — интеграция не настроена или не реализована (вызывающий код
    показывает понятное сообщение вместо падения). Если нужно объяснить
    пользователю КОНКРЕТНО, чего не хватает — см. unsupported_reason()
    ниже, вызывается отдельно (get_client() сам не формирует текст, чтобы
    не путать «клиент не построен» с «объяснение причины»)."""
    if counterparty.api_provider not in SUPPORTED_PROVIDERS:
        return None

    cred = counterparty.api_credential
    if cred is None or not cred.login or not cred.secret_encrypted:
        return None
    secret = crypto.decrypt(cred.secret_encrypted)
    if not secret:
        return None

    if counterparty.api_provider == CounterpartyApiProvider.SMSAERO:
        return SmsAeroBalanceClient(cred.login, secret)
    if counterparty.api_provider == CounterpartyApiProvider.BEGET:
        return BegetBalanceClient(cred.login, secret)
    if counterparty.api_provider == CounterpartyApiProvider.TNS_ENERGO_BUSINESS:
        if not cred.extra:
            return None
        return TnsEnergoBusinessBalanceClient(cred.login, secret, cred.extra)

    return None


def unsupported_reason(counterparty: Counterparty) -> str:
    """Человекочитаемое объяснение, ПОЧЕМУ get_client(counterparty) вернул
    None — чтобы председатель/правление сами понимали, чего не хватает
    (не выбран провайдер? не сохранились логин/пароль? для ТНС-Энерго
    Бизнес — не указан регион?), не гадая и не прося разработчика лезть в
    БД. Вызывать только когда уже известно, что get_client() дал None —
    сама по себе не проверяет, поддержан ли провайдер вообще успешно."""
    if counterparty.api_provider == CounterpartyApiProvider.NONE:
        return _("не выбран API — установите его в настройках контрагента")
    if counterparty.api_provider not in SUPPORTED_PROVIDERS:
        return _("этот провайдер выбран, но пока не реализован")

    cred = counterparty.api_credential
    if cred is None or not cred.login:
        return _("не указан логин — заполните его кнопкой «Настроить API»")
    if not cred.secret_encrypted:
        return _("не указан пароль/API-ключ — заполните его кнопкой «Настроить API»")
    if crypto.decrypt(cred.secret_encrypted) is None:
        return _("не удалось расшифровать сохранённый секрет — сохраните пароль/API-ключ заново")
    if counterparty.api_provider == CounterpartyApiProvider.TNS_ENERGO_BUSINESS and not cred.extra:
        return _("не указан регион (поддомен личного кабинета) — заполните его кнопкой «Настроить API»")

    return _("неизвестная причина")
