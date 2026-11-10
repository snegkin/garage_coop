#!/bin/sh
# Обёртка для запуска backup_offsite.py по cron — после backup_db и
# backup_mailbox:
#   20 3 * * * /path/to/project/scripts/backup_offsite.sh
#
# Без RESTIC_REPOSITORY в .env только обновляет instance/backup_staging/
# (его забирает агент на компьютере председателя, см. backup_agent/).
# Лог — <INSTANCE_DIR>/logs/backup_offsite.log.

set -eu

. "$(dirname "$0")/_common.sh"

run_locked backup_offsite
