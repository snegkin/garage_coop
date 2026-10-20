#!/bin/sh
# Обёртка для запуска sync_counterparty_balances.py по cron.
#
# Пример строки в crontab (каждый день в 7:10 по времени сервера — запуск
# ежедневный, а как часто реально обновлять баланс, решает интервал в
# карточке контрагента):
#   10 7 * * * /path/to/project/scripts/sync_counterparty_balances.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/sync_counterparty_balances.log — сама
# папка logs/ будет создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked sync_counterparty_balances
