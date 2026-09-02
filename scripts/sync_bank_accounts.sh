#!/bin/sh
# Обёртка для запуска sync_bank_accounts.py по cron.
#
# Пример строки в crontab (только по будням, в 7:00 по времени сервера —
# до начала рабочего дня председателя/бухгалтера, банк к этому времени
# обычно уже провёл вчерашние операции):
#   0 7 * * 1-5 /path/to/project/scripts/sync_bank_accounts.sh
#
# Логи копятся в instance/logs/sync_bank_accounts.log — сама папка logs/
# будет создана при первом запуске, если её ещё нет.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. Веб-процесс обычно получает SECRET_KEY/
# BANK_API_ENCRYPTION_KEY через окружение сервиса (systemd EnvironmentFile=
# и т.п.), а этот скрипт из-под cron — нет, поэтому подгружаем .env сами:
# без совпадающего ключа расшифровка сохранённых client_secret/refresh_token
# банка здесь не сойдётся с тем, чем они были зашифрованы в веб-процессе
# (тот же приём, что и в poll_ewelink.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/sync_bank_accounts.log"
LOCK_FILE="$PROJECT_DIR/instance/sync_bank_accounts.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# flock -n — чтобы ручной запуск и cron день в день не полезли одновременно
# писать в БД; если предыдущий запуск почему-то ещё не завершился, новый
# просто пропускается, а не встаёт в очередь.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/sync_bank_accounts.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/sync_bank_accounts.py" >> "$LOG_FILE" 2>&1
fi
