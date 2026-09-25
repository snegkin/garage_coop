#!/bin/sh
# Обёртка для запуска poll_telegram.py по cron.
#
# Пример строки в crontab (опрос раз в минуту):
#   * * * * * /path/to/project/scripts/poll_telegram.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/poll_telegram.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет. Лог-файл при опросе раз в
# минуту растёт быстро — см. docs/deployment.md, раздел «Автоматизация», про logrotate.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked poll_telegram
