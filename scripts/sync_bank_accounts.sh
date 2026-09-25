#!/bin/sh
# Обёртка для запуска sync_bank_accounts.py по cron.
#
# Пример строки в crontab (только по будням, в 7:00 по времени сервера —
# до начала рабочего дня председателя/бухгалтера, банк к этому времени
# обычно уже провёл вчерашние операции):
#   0 7 * * 1-5 /path/to/project/scripts/sync_bank_accounts.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/sync_bank_accounts.log — сама папка logs/
# будет создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked sync_bank_accounts
