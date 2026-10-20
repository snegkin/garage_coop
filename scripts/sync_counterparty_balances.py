#!/usr/bin/env python3
"""
Автоматическое обновление баланса личного кабинета контрагентов с
настроенным API (см. app/counterparty_api/: SMS Aero, Beget.com,
ТНС-Энерго Бизнес) — для запуска по cron (см.
scripts/sync_counterparty_balances.sh и docs/deployment.md, раздел
«Автоматизация»).

Раньше жил внутри scripts/sync_bank_accounts.py одним прогоном с
расчётными счетами — вынесен, чтобы у него был свой лог и свой код
возврата (ошибка ЛК контрагента не делает неуспешной синхронизацию банка).

Использует ту же логику, что и кнопка «Обновить баланс» на карточке
контрагента (app/counterparty_sync.py: sync_counterparty_balance) — не
дублирует её, чтобы поведение не расходилось. Контрагенты без выбранного
api_provider (баланс их личного кабинета не отслеживается) молча
пропускаются; у остальных баланс обновляется не на каждом запуске, а с
интервалом из карточки (balance_sync_interval_days, см. balance_sync_due).

Запуск вручную:
    cd /path/to/project && python3 scripts/sync_counterparty_balances.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database, notifications
from app.models import Counterparty, CounterpartyApiProvider
from app.counterparty_sync import balance_sync_due, sync_counterparty_balance


def _log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}")


def main() -> int:
    app = create_app()
    with app.app_context():
        had_errors = _sync_counterparties()
        # Уведомления (notify_failure) — вне запроса after_request их не отправит.
        notifications.dispatch_after_commit()
        return 1 if had_errors else 0


def _sync_counterparties() -> bool:
    """Возвращает True, если хотя бы одна синхронизация завершилась ошибкой."""
    counterparties = (
        database.db_session.query(Counterparty)
        .filter(Counterparty.api_provider != CounterpartyApiProvider.NONE)
        .order_by(Counterparty.id)
        .all()
    )
    if not counterparties:
        _log("Контрагентов с настроенным API нет — баланс личного кабинета синхронизировать нечего.")
        return False

    had_errors = False
    for counterparty in counterparties:
        if not balance_sync_due(counterparty):
            # Интервал обновления (balance_sync_interval_days) ещё не
            # прошёл — молча, как и ненастроенные: иначе лог каждого
            # прогона состоял бы из строк «пропущено».
            continue
        status, message = sync_counterparty_balance(counterparty)
        if status == "unsupported":
            # Провайдер выбран, но не настроен (например, не заполнены
            # креды) — не ошибка, молча пропускаем, как и у банков.
            continue
        if status == "error":
            had_errors = True
            _log(f"{counterparty.name}: баланс личного кабинета — ОШИБКА: {message}")
            notifications.notify_failure(
                f"counterparty_api:{counterparty.id}", f"баланс личного кабинета {counterparty.name}", message,
            )
        else:
            _log(f"{counterparty.name}: баланс личного кабинета — {message}")
    return had_errors


if __name__ == "__main__":
    sys.exit(main())
