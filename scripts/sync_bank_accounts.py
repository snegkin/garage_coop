#!/usr/bin/env python3
"""
Автоматическая синхронизация всех расчётных счетов кооператива с API
банка — баланс и выписка за последние STATEMENT_SYNC_DAYS дней, для
запуска по cron в рабочие дни (см. scripts/sync_bank_accounts.sh и
README.md, раздел «Автоматизация»).

По аналогии с scripts/update_key_rate.py: отдельный скрипт, а не фоновый
поток внутри веб-процесса. Использует ту же логику, что и кнопки
«Обновить баланс»/«Загрузить из банка» на странице расчётных счетов
(app/bank_sync.py: sync_account_balance/sync_account_statement) — не
дублирует её, чтобы поведение не расходилось.

Окно выписки — последние 7 дней, а не только «со вчера»: banki иногда
проводят операции с задержкой на выходные/праздники, и повторная
загрузка уже известных операций безвредна (дедуп по external_uid внутри
sync_account_statement — см. её докстринг). Дни без выписки (пока у
счёта не настроена интеграция) просто пропускаются — это ожидаемое
состояние, не ошибка.

Запуск вручную:
    cd /path/to/project && python3 scripts/sync_bank_accounts.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.models import BankAccount
from app.bank_sync import sync_account_balance, sync_account_statement

STATEMENT_SYNC_DAYS = 7


def _log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}")


def main() -> int:
    app = create_app()
    with app.app_context():
        accounts = database.db_session.query(BankAccount).order_by(BankAccount.id).all()
        if not accounts:
            _log("Расчётных счетов в справочнике нет — синхронизировать нечего.")
            return 0

        date_to = dt.date.today()
        date_from = date_to - dt.timedelta(days=STATEMENT_SYNC_DAYS)

        configured = 0
        had_errors = False
        for account in accounts:
            label = f"{account.bank_name} {account.checking_account} (id={account.id})"

            balance_status, balance_message = sync_account_balance(account)
            if balance_status == "unsupported":
                # Для этого счёта интеграция не настроена вовсе — не ошибка,
                # просто пропускаем и баланс, и выписку молча (без лишнего
                # шума в логе на каждый некофигурированный счёт).
                continue
            configured += 1
            if balance_status == "error":
                had_errors = True
                _log(f"{label}: баланс — ОШИБКА: {balance_message}")
            else:
                _log(f"{label}: баланс — {balance_message}")

            statement_status, statement_message, stats = sync_account_statement(account, date_from, date_to)
            if statement_status == "error":
                had_errors = True
                _log(f"{label}: выписка за {date_from}—{date_to} — ОШИБКА: {statement_message}")
            elif statement_status == "success":
                extra = ""
                if stats["auto_allocated"]:
                    extra = f", из них {stats['auto_allocated']} разнесено автоматически"
                if stats["direct"] or stats["parametric"]:
                    extra += f"; сопоставлено с реестром: {stats['direct']} прямых + {stats['parametric']} параметрических"
                _log(f"{label}: выписка за {date_from}—{date_to} — {stats['added']} новых операций{extra}.")
            # statement_status == "unsupported" здесь не ожидается отдельно от
            # баланса (get_client одинаково решает для обоих), но на всякий
            # случай тоже не считается ошибкой.

        if configured == 0:
            _log("Ни у одного счёта не настроена интеграция с API банка — синхронизировать нечего.")
        return 1 if had_errors else 0


if __name__ == "__main__":
    sys.exit(main())
