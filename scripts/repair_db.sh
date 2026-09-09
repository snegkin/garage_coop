#!/bin/sh
# Обёртка для scripts/repair_db.py — запускать ПЕРЕД стартом веб-процесса
# (systemd ExecStartPre=), см. README.md, раздел «Автоматизация».
#
# Пример юнита (фрагмент, добавить в уже существующий systemd-юнит
# веб-процесса — своего файла юнита в этом репозитории нет, он настраивается
# отдельно на сервере):
#   [Service]
#   ExecStartPre=/path/to/project/scripts/repair_db.sh
#   ExecStart=... (gunicorn/uwsgi и т.п.)
#
# Ненулевой код выхода ExecStartPre не даёт systemd запустить сам сервис —
# если REINDEX не смог починить БД (повреждены сами данные, не только
# индексы), это осознанно: лучше не запускать сервис на битой БД, чем
# отдавать 500-е, а решение (восстановить из бэкапа или разбираться руками)
# должен принять человек, не cron/systemd.
#
# Логи копятся в instance/logs/repair_db.log.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv — приложение читает os.environ
# напрямую (см. .env.example). Без этого блока при переопределённом
# DATABASE_PATH скрипт проверял бы не ту БД (тот же приём, что и в
# backup_db.sh/restore_db.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/repair_db.log"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# Без flock: рассчитан на запуск как ExecStartPre одного экземпляра
# сервиса, не на cron — параллельного запуска этим же механизмом не бывает.
exec "$PYTHON" "$SCRIPT_DIR/repair_db.py" >> "$LOG_FILE" 2>&1
