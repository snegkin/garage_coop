#!/bin/sh
# Обёртка для запуска dvr_snapshot.py по cron.
#
# Пример строки в crontab (раз в минуту):
#   * * * * * /path/to/project/scripts/dvr_snapshot.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/dvr_snapshot.log — растёт быстро при запуске
# раз в минуту, стоит добавить в logrotate (см. пример для poll_ewelink.log
# в docs/deployment.md, «Автоматизация»).

set -eu

. "$(dirname "$0")/_common.sh"

run_locked dvr_snapshot
