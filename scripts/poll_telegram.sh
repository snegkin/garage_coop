#!/bin/sh
# Обёртка для запуска poll_telegram.py по cron.
#
# Пример строки в crontab (опрос раз в минуту):
#   * * * * * /path/to/project/scripts/poll_telegram.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/poll_telegram.log и ротируются по размеру
# (rotate_log в _common.sh) — logrotate не нужен.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked poll_telegram
