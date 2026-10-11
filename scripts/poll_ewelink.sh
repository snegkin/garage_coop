#!/bin/sh
# Обёртка для запуска poll_ewelink.py по cron.
#
# Пример строки в crontab (опрос раз в минуту — cron поддерживает минутную
# гранулярность нативно, отдельный планировщик внутри приложения не нужен):
#   * * * * * /path/to/project/scripts/poll_ewelink.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/poll_ewelink.log и ротируются по размеру
# (rotate_log в _common.sh) — logrotate не нужен.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked poll_ewelink
