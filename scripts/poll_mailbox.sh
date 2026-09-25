#!/bin/sh
# Обёртка для запуска poll_mailbox.py по cron.
#
# Пример строки в crontab (раз в 5 минут — свежесть бейджа "непрочитано" не
# требует минутной гранулярности, в отличие от poll_ewelink.py):
#   */5 * * * * /path/to/project/scripts/poll_mailbox.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/poll_mailbox.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked poll_mailbox
