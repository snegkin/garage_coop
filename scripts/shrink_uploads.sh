#!/bin/sh
# Разовое ужатие уже загруженных фото (scripts/shrink_uploads.py), с .env
# и venv, как у cron-обёрток. Не cron: новые загрузки ужимаются сами.
#   scripts/shrink_uploads.sh --dry-run
#   scripts/shrink_uploads.sh
# Вывод — в консоль и в <INSTANCE_DIR>/logs/shrink_uploads.log.

set -eu

. "$(dirname "$0")/_common.sh"

"$PYTHON" "$SCRIPT_DIR/shrink_uploads.py" "$@" 2>&1 | tee -a "$LOG_DIR/shrink_uploads.log"
