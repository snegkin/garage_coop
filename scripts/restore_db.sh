#!/bin/sh
# Обёртка для scripts/restore_db.py — подхватывает venv проекта, как и
# backup_db.sh. В отличие от бэкапа, восстановление НЕ предназначено для
# cron: это ручная операция. Обёртка нужна просто чтобы не думать о venv.
#
# Примеры:
#   ./scripts/restore_db.sh --list
#   ./scripts/restore_db.sh --restore latest
#   ./scripts/restore_db.sh --restore 3
#   ./scripts/restore_db.sh --restore coop_20260829_030000.db
#
# Подтверждение перезаписи текущей БД запрашивается интерактивно, если не
# передан --yes.

set -eu

. "$(dirname "$0")/_common.sh"

# Ручная операция, не cron — без flock и без общей блокировки.
exec "$PYTHON" "$SCRIPT_DIR/restore_db.py" "$@"
