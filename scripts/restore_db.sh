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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. restore_db.py берёт DATABASE_PATH/BACKUP_DIR из
# окружения (см. сам скрипт и backup_db.sh) — без .env восстановление могло
# бы тихо перезаписать не ту БД, если пути переопределены.
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

exec "$PYTHON" "$SCRIPT_DIR/restore_db.py" "$@"
