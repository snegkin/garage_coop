#!/bin/sh
# Обёртка для запуска poll_mailbox.py по cron.
#
# Пример строки в crontab (раз в 5 минут — свежесть бейджа "непрочитано" не
# требует минутной гранулярности, в отличие от poll_ewelink.py):
#   */5 * * * * /path/to/project/scripts/poll_mailbox.sh
#
# Логи копятся в instance/logs/poll_mailbox.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. Веб-процесс обычно получает SECRET_KEY/
# BANK_API_ENCRYPTION_KEY через окружение сервиса (systemd EnvironmentFile=
# и т.п.), а этот скрипт из-под cron — нет, поэтому подгружаем .env сами:
# без совпадающего ключа расшифровка сохранённого пароля почты здесь не
# сойдётся с тем, чем он был зашифрован в веб-процессе.
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/poll_mailbox.log"
LOCK_FILE="$PROJECT_DIR/instance/poll_mailbox.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# flock -n (неблокирующий): если предыдущий запуск ещё не завершился
# (например, почтовый сервер подвис на таймауте) — новый запуск просто
# пропускается, а не встаёт в очередь и не накапливается.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/poll_mailbox.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/poll_mailbox.py" >> "$LOG_FILE" 2>&1
fi
