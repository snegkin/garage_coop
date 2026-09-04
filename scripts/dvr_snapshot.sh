#!/bin/sh
# Обёртка для запуска dvr_snapshot.py по cron.
#
# Пример строки в crontab (раз в минуту):
#   * * * * * /path/to/project/scripts/dvr_snapshot.sh
#
# Логи копятся в instance/logs/dvr_snapshot.log — растёт быстро при запуске
# раз в минуту, стоит добавить в logrotate (см. пример для poll_ewelink.log
# в README.md, «Автоматизация»).

set -eu

# Директория самого скрипта -> корень проекта на уровень выше.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. Веб-процесс обычно получает SECRET_KEY/
# BANK_API_ENCRYPTION_KEY через окружение сервиса (systemd EnvironmentFile=
# и т.п.), а этот скрипт из-под cron — нет, поэтому подгружаем .env сами:
# без совпадающего ключа расшифровка сохранённого пароля регистратора
# (DvrRecorder.password_encrypted, см. app/surveillance.py:rtsp_url) здесь
# не сойдётся с тем, чем он был зашифрован в веб-процессе (тот же приём,
# что и в poll_ewelink.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/dvr_snapshot.log"
LOCK_FILE="$PROJECT_DIR/instance/dvr_snapshot.lock"
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

# flock — чтобы прогон, который тормозит на недоступной камере, не
# накладывался на следующий запуск минутой позже.
if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/dvr_snapshot.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/dvr_snapshot.py" >> "$LOG_FILE" 2>&1
fi
