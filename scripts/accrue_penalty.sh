#!/bin/sh
# Обёртка для запуска accrue_penalty.py по cron.
#
# Пример строки в crontab (1-го числа каждого месяца, в 6:10 по времени
# сервера — чуть позже update_key_rate.sh, чтобы ставка ЦБ РФ на начало
# месяца уже точно была подтянута):
#   10 6 1 * * /path/to/project/scripts/accrue_penalty.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/accrue_penalty.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked accrue_penalty
