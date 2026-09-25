#!/bin/sh
# Обёртка для запуска update_key_rate.py по cron.
#
# Пример строки в crontab (обновление раз в сутки, в 6:00 по времени сервера —
# ЦБ РФ публикует изменения ставки днём, ночной или ранний запуск достаточен):
#   0 6 * * * /path/to/project/scripts/update_key_rate.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/update_key_rate.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked update_key_rate
