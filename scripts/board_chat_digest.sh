#!/bin/sh
# Обёртка для запуска board_chat_digest.py по cron.
#
# Пример строки в crontab (каждые 5 минут — порог "не прочитано 10 минут"
# требует более частой проверки, чем ежемесячные/ежедневные скрипты):
#   */5 * * * * /path/to/project/scripts/board_chat_digest.sh
#
# Логи копятся в instance/logs/board_chat_digest.log — сама папка logs/
# будет создана при первом запуске, если её ещё нет.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv — приложение читает os.environ
# напрямую. Веб-процесс обычно получает переменные через окружение сервиса
# (systemd EnvironmentFile= и т.п.), а у cron своего окружения нет — без
# этого блока скрипт тихо взял бы дефолты из config.py вместо настоящих
# продовых значений (тот же приём, что и в accrue_penalty.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/board_chat_digest.log"
LOCK_FILE="$PROJECT_DIR/instance/board_chat_digest.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# flock — чтобы при запуске каждые 5 минут медленный прогон не пересёкся
# со следующим (например, если письмо отправляется на медленный SMTP).
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/board_chat_digest.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/board_chat_digest.py" >> "$LOG_FILE" 2>&1
fi
