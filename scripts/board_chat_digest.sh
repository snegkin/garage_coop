#!/bin/sh
# Обёртка для запуска board_chat_digest.py по cron.
#
# Пример строки в crontab (каждые 5 минут — порог "не прочитано 10 минут"
# требует более частой проверки, чем ежемесячные/ежедневные скрипты):
#   */5 * * * * /path/to/project/scripts/board_chat_digest.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/board_chat_digest.log — сама папка logs/
# будет создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked board_chat_digest
