"""
Общий интерфейс клиента API банка — одинаковый для всех банков, чтобы
app/bank_sync.py не знал, с каким именно банком работает. Сейчас реализован
только SberbankClient (см. sberbank.py); ВТБ и Т-Банк — см. app/bank_api/__init__.py.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt
from decimal import Decimal


class BankApiError(Exception):
    """Любая ошибка обращения к API банка — сетевая, авторизации, формата ответа.
    app/bank_sync.py ловит именно этот тип и сохраняет str(e) в BankApiCredential.last_error,
    не давая упасть всему запросу (синхронизация может не получиться, это не баг приложения)."""


@dataclasses.dataclass
class BalanceInfo:
    amount: Decimal
    as_of: dt.date


@dataclasses.dataclass
class StatementLine:
    external_uid: str | None
    operation_date: dt.date
    direction: str  # "credit" | "debit"
    amount: Decimal
    counterparty_name: str | None = None
    counterparty_inn: str | None = None
    payment_purpose: str | None = None
    document_number: str | None = None


@dataclasses.dataclass
class ChargeRegistryItem:
    """Одно начисление для отправки в реестр начислений."""
    account_number: str  # лицевой счёт плательщика (MemberAccount/PersonalAccount.account_number)
    payer_name: str
    amount: Decimal
    purpose: str
    document_number: str | None = None
    service_code: str | None = None  # код услуги/периода — см. registry_file.RegistryFormat.service_code


@dataclasses.dataclass
class ChargeRegistryResult:
    external_id: str
    status: str
    bank_comment: str | None = None


@dataclasses.dataclass
class PaymentRegistryItem:
    external_id: str
    account_number: str | None
    payer_name: str | None
    amount: Decimal  # сумма начисления, которую гасит платёж (для FIFO-разнесения) — НЕ сумма зачисления за вычетом комиссии
    operation_date: dt.date
    payment_purpose: str | None = None
    credited_amount: Decimal | None = None  # реально зачислено кооперативу (за вычетом комиссии банка)
    fee_amount: Decimal | None = None  # комиссия банка, удержанная из платежа


class BankApiClient(abc.ABC):
    """
    Интерфейс, который должен реализовать клиент любого банка. Методы
    поднимают BankApiError при любой проблеме — не возвращают None/пустой
    список молча, чтобы app/bank_sync.py мог показать председателю причину,
    а не тихо «ничего не обновилось».
    """

    @abc.abstractmethod
    def get_balance(self) -> BalanceInfo:
        """Текущий остаток по счёту."""

    @abc.abstractmethod
    def get_statement(self, date_from: dt.date, date_to: dt.date) -> list[StatementLine]:
        """Операции (зачисления и списания) за период, включительно с обеих сторон."""

    @abc.abstractmethod
    def send_charge_registry(self, items: list[ChargeRegistryItem], period: str) -> ChargeRegistryResult:
        """Отправляет реестр начислений в банк, возвращает присвоенный банком external_id и статус приёма."""

    @abc.abstractmethod
    def get_charge_registry_status(self, external_id: str) -> ChargeRegistryResult:
        """Текущий статус ранее отправленного реестра начислений."""
