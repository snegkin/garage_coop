#!/bin/sh
# Обёртка для запуска backup_db.py по cron.
#
# Пример строки в crontab (ежедневный бэкап в 3:00 ночи по времени сервера,
# когда меньше всего активной записи в БД):
#   0 3 * * * /path/to/project/scripts/backup_db.sh
#
# Логи копятся в instance/logs/backup_db.log — сама папка logs/ будет
# создана при первом запуске, если её ещё нет.
#
# ВАЖНО: бэкап лежит на том же диске, что и сама БД — это защита от
# повреждения файла/случайного удаления, НЕ от отказа всего сервера/диска
# целиком. Для реальной защиты не забудьте синхронизировать instance/backups/
# куда-то ещё (rsync на другой сервер, облачное хранилище и т.п.) —
# этот скрипт сам по себе такую синхронизацию не делает.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# .env НЕ подхватывается python-dotenv (см. .env.example) — приложение читает
# os.environ напрямую. backup_db.py берёт DATABASE_PATH/BACKUP_DIR из
# окружения (см. сам скрипт) — если они переопределены в .env (БД не в
# стандартном instance/coop.db), без этого блока бэкап тихо снимался бы не
# с той БД (тот же приём, что и в poll_ewelink.sh).
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/instance/logs"
LOG_FILE="$LOG_DIR/backup_db.log"
LOCK_FILE="$PROJECT_DIR/instance/backup_db.lock"
mkdir -p "$LOG_DIR"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

if command -v flock >/dev/null 2>&1; then
    exec flock -n "$LOCK_FILE" "$PYTHON" "$SCRIPT_DIR/backup_db.py" >> "$LOG_FILE" 2>&1
else
    exec "$PYTHON" "$SCRIPT_DIR/backup_db.py" >> "$LOG_FILE" 2>&1
fi
