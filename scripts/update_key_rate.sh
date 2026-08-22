#!/bin/sh
# Обёртка для запуска update_key_rate.py по cron.
#
# Пример строки в crontab (обновление раз в сутки, в 6:00 по времени сервера —
# ЦБ РФ публикует изменения ставки днём, ночной или ранний запуск достаточен):
#   0 6 * * * /path/to/project/scripts/update_key_rate.sh
#
# Логи копятся в instance/logs/update_key_rate.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.

set -eu

# Директория самого скрипта -> корень проекта на уровень выше.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/update_key_rate.log"
LOCK_FILE="$PROJECT_DIR/instance/update_key_rate.lock"
mkdir -p "$LOG_DIR"

# Если в проекте есть venv (.venv или venv) — используем его python; иначе
# берём тот, что первым найдётся в PATH.
if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# flock — чтобы два одновременных запуска (например, ручной + cron день в
# день) не полезли одновременно писать в БД; если flock недоступен в системе
# (не всегда есть на минимальных образах) — просто выполняем без блокировки.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/update_key_rate.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/update_key_rate.py" >> "$LOG_FILE" 2>&1
fi
