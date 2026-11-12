#!/bin/sh
# Обёртка для запуска poll_messengers.py по cron.
#
# Пример строки в crontab (опрос раз в минуту):
#   * * * * * /path/to/project/scripts/poll_messengers.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/poll_messengers.log и ротируются по размеру
# (rotate_log в _common.sh) — logrotate не нужен.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked poll_messengers
