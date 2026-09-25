#!/bin/sh
# Обёртка для запуска reconcile_sms_charges.py по cron.
#
# Пример строки в crontab (раз в сутки, ночью — SMS Aero обычно считает
# стоимость с задержкой, чаще проверять смысла нет):
#   30 3 * * * /path/to/project/scripts/reconcile_sms_charges.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/reconcile_sms_charges.log — сама папка logs/
# будет создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked reconcile_sms_charges
