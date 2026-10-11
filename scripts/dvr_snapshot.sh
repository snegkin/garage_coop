#!/bin/sh
# Обёртка для запуска dvr_snapshot.py по cron.
#
# Пример строки в crontab (раз в минуту):
#   * * * * * /path/to/project/scripts/dvr_snapshot.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/dvr_snapshot.log и ротируются по размеру
# (rotate_log в _common.sh) — logrotate не нужен.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked dvr_snapshot
