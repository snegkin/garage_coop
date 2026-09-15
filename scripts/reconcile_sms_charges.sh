#!/bin/sh
# Обёртка для запуска reconcile_sms_charges.py по cron.
#
# Пример строки в crontab (раз в сутки, ночью — SMS Aero обычно считает
# стоимость с задержкой, чаще проверять смысла нет):
#   30 3 * * * /path/to/project/scripts/reconcile_sms_charges.sh
#
# Логи копятся в instance/logs/reconcile_sms_charges.log — сама папка logs/
# будет создана при первом запуске, если её ещё нет.

set -eu

# Директория самого скрипта -> корень проекта на уровень выше.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. Веб-процесс обычно получает SECRET_KEY/шифрование
# через окружение сервиса (systemd EnvironmentFile= и т.п.), а этот скрипт
# из-под cron — нет: без .env расшифровка сохранённого API-ключа SMS Aero
# здесь не сойдётся с тем, чем он был зашифрован в веб-процессе (тот же
# приём, что и в sync_bank_accounts.sh/poll_ewelink.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/reconcile_sms_charges.log"
LOCK_FILE="$PROJECT_DIR/instance/reconcile_sms_charges.lock"
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

# flock -n — чтобы ручной запуск и cron день в день не полезли одновременно
# писать в БД; если предыдущий запуск почему-то ещё не завершился, новый
# просто пропускается, а не встаёт в очередь.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/reconcile_sms_charges.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/reconcile_sms_charges.py" >> "$LOG_FILE" 2>&1
fi
