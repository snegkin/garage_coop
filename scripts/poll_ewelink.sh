#!/bin/sh
# Обёртка для запуска poll_ewelink.py по cron.
#
# Пример строки в crontab (опрос раз в минуту — cron поддерживает минутную
# гранулярность нативно, отдельный планировщик внутри приложения не нужен):
#   * * * * * /path/to/project/scripts/poll_ewelink.sh
#
# Логи копятся в instance/logs/poll_ewelink.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет. Лог-файл при опросе раз в
# минуту растёт быстро — см. README.md, раздел «Автоматизация», про logrotate.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/poll_ewelink.log"
LOCK_FILE="$PROJECT_DIR/instance/poll_ewelink.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# flock -n (неблокирующий): если предыдущий запуск ещё не завершился к
# началу следующей минуты (например, eWeLink подвис на таймауте) — новый
# запуск просто пропускается, а не встаёт в очередь и не накапливается.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/poll_ewelink.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/poll_ewelink.py" >> "$LOG_FILE" 2>&1
fi
