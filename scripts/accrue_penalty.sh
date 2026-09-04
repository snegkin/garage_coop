#!/bin/sh
# Обёртка для запуска accrue_penalty.py по cron.
#
# Пример строки в crontab (1-го числа каждого месяца, в 6:10 по времени
# сервера — чуть позже update_key_rate.sh, чтобы ставка ЦБ РФ на начало
# месяца уже точно была подтянута):
#   10 6 1 * * /path/to/project/scripts/accrue_penalty.sh
#
# Логи копятся в instance/logs/accrue_penalty.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.

set -eu

# Директория самого скрипта -> корень проекта на уровень выше.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. Веб-процесс обычно получает DATABASE_URL/SECRET_KEY
# через окружение сервиса (systemd EnvironmentFile= и т.п.), а этот скрипт
# из-под cron — нет: без .env create_app() тихо возьмёт дефолтную БД из
# config.py вместо настоящей продовой (тот же приём, что и в poll_ewelink.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/accrue_penalty.log"
LOCK_FILE="$PROJECT_DIR/instance/accrue_penalty.lock"
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
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/accrue_penalty.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/accrue_penalty.py" >> "$LOG_FILE" 2>&1
fi
