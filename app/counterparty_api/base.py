"""
Общий интерфейс клиента API контрагента — одинаковый для всех провайдеров,
чтобы app/counterparty_sync.py не знал, с каким именно контрагентом
работает (тот же приём, что app/bank_api/base.py для банков). Сейчас
реализован только SmsAeroBalanceClient (см. smsaero.py).
"""
from __future__ import annotations

import abc

from ..bank_api.base import BalanceInfo  # {amount: Decimal, as_of: dt.date} — generic, переиспользуется как есть


class CounterpartyApiError(Exception):
    """Любая ошибка обращения к API контрагента — сетевая, авторизации,
    формата ответа. app/counterparty_sync.py ловит именно этот тип и
    сохраняет str(e) в Counterparty.external_balance_error, не давая
    упасть всему запросу (синхронизация может не получиться, это не баг
    приложения)."""


class CounterpartyApiClient(abc.ABC):
    @abc.abstractmethod
    def get_balance(self) -> BalanceInfo:
        """Текущий баланс личного кабинета контрагента у провайдера (не
        баланс расчётов кооператива с самим контрагентом)."""
