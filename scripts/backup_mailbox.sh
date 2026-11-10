#!/bin/sh
# Обёртка для запуска backup_mailbox.py по cron — до backup_offsite:
#   50 2 * * * /path/to/project/scripts/backup_mailbox.sh
#
# Лог — <INSTANCE_DIR>/logs/backup_mailbox.log.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked backup_mailbox
