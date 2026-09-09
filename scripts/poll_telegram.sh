#!/bin/sh
# Обёртка для запуска poll_telegram.py по cron.
#
# Пример строки в crontab (опрос раз в минуту):
#   * * * * * /path/to/project/scripts/poll_telegram.sh
#
# Логи копятся в instance/logs/poll_telegram.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет. Лог-файл при опросе раз в
# минуту растёт быстро — см. README.md, раздел «Автоматизация», про logrotate.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv — приложение читает os.environ
# напрямую. Веб-процесс обычно получает SECRET_KEY/BANK_API_ENCRYPTION_KEY
# через окружение сервиса, а у cron своего окружения нет — без этого блока
# расшифровка сохранённого токена бота здесь не сошлась бы с тем, чем его
# зашифровал веб-процесс (тот же приём, что и в poll_ewelink.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/poll_telegram.log"
LOCK_FILE="$PROJECT_DIR/instance/poll_telegram.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# flock -n — если предыдущий запуск ещё не завершился к началу следующей
# минуты, новый просто пропускается, а не встаёт в очередь.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/poll_telegram.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/poll_telegram.py" >> "$LOG_FILE" 2>&1
fi
