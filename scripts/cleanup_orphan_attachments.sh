#!/bin/sh
# Обёртка для запуска cleanup_orphan_attachments.py по cron.
#
# Пример строки в crontab (раз в сутки достаточно — порог «осиротелости»
# и так 24 часа, чаще запускать нет смысла):
#   30 3 * * * /path/to/project/scripts/cleanup_orphan_attachments.sh
#
# Логи копятся в instance/logs/cleanup_orphan_attachments.log — сама папка
# logs/ будет создана при первом запуске, если её ещё нет.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/cleanup_orphan_attachments.log"
LOCK_FILE="$PROJECT_DIR/instance/cleanup_orphan_attachments.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/cleanup_orphan_attachments.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/cleanup_orphan_attachments.py" >> "$LOG_FILE" 2>&1
fi
