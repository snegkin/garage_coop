#!/bin/sh
# Обёртка для запуска cleanup_orphan_attachments.py по cron.
#
# Пример строки в crontab (раз в сутки достаточно — порог «осиротелости»
# и так 24 часа, чаще запускать нет смысла):
#   30 3 * * * /path/to/project/scripts/cleanup_orphan_attachments.sh
#
# Логи копятся в <INSTANCE_DIR>/logs/cleanup_orphan_attachments.log — сама папка
# logs/ будет создана при первом запуске, если её ещё нет.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked cleanup_orphan_attachments
