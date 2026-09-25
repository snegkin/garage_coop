# Общая часть обёрток scripts/*.sh — подключается из них через
#   . "$(dirname "$0")/_common.sh"
# (не запускается сама по себе). Задаёт SCRIPT_DIR, PROJECT_DIR,
# INSTANCE_DIR, LOG_DIR, PYTHON и функцию run_locked.
#
# Окружение. .env НЕ подхватывается python-dotenv (см. .env.example) —
# приложение читает os.environ напрямую. Веб-процесс получает переменные
# через окружение сервиса (systemd EnvironmentFile= и т.п.), а у cron своего
# окружения нет — поэтому подгружаем .env сами. Без этого скрипт тихо взял
# бы дефолты из config.py: не ту БД/каталог загрузок, а SECRET_KEY/
# BANK_API_ENCRYPTION_KEY не совпали бы с веб-процессом, и расшифровка
# сохранённых секретов (токены банка/eWeLink/Telegram, пароли почты и
# регистраторов, ключ SMS Aero) не сошлась бы с тем, чем их зашифровал сайт.
#
# Несколько кооперативов с одного checkout'а кода (docs/deployment.md,
# «Несколько кооперативов на одном сервере»): у каждого свой .env с
# собственным INSTANCE_DIR, путь к нему передаётся в ENV_FILE, например
# в crontab:
#   ENV_FILE=/home/coop2/coop.env /path/to/project/scripts/backup_db.sh
# Явно указанный, но отсутствующий ENV_FILE — ошибка, а не молчаливый откат
# на <проект>/.env (иначе скрипт второго кооператива тихо работал бы с БД
# первого).
#
# Общая блокировка на весь сервер (COOP_CRON_LOCK, необязательно) — на
# маленьком сервере (1 ГБ ОЗУ) каждый скрипт поднимает приложение целиком
# (~120 МБ); с ней одновременно выполняется не больше одного скрипта всех
# кооперативов, остальные ждут до COOP_CRON_LOCK_TIMEOUT секунд (по
# умолчанию 600), затем пропускают запуск. Файл блокировки должен быть
# доступен всем пользователям кооперативов (см. docs/deployment.md).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if [ -n "${ENV_FILE:-}" ]; then
    if [ ! -f "$ENV_FILE" ]; then
        echo "ENV_FILE не найден: $ENV_FILE" >&2
        exit 1
    fi
else
    ENV_FILE="$PROJECT_DIR/.env"
fi
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

# Те же правила, что и в config.py/scripts/_paths.py.
INSTANCE_DIR="${INSTANCE_DIR:-$PROJECT_DIR/instance}"
export INSTANCE_DIR
LOG_DIR="$INSTANCE_DIR/logs"
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

# run_locked <имя> [аргументы] — запускает scripts/<имя>.py с логом в
# $LOG_DIR/<имя>.log. flock -n на $INSTANCE_DIR/<имя>.lock: если предыдущий
# запуск этого же скрипта этого же кооператива ещё не завершился (подвис на
# таймауте внешнего сервиса, ручной запуск + cron день в день), новый просто
# пропускается, а не встаёт в очередь и не копится. Если flock в системе нет
# (минимальные образы) — выполняем без блокировок.
run_locked() {
    name="$1"
    shift
    log_file="$LOG_DIR/$name.log"
    if ! command -v flock >/dev/null 2>&1; then
        exec "$PYTHON" "$SCRIPT_DIR/$name.py" "$@" >> "$log_file" 2>&1
    fi
    if [ -n "${COOP_CRON_LOCK:-}" ]; then
        exec flock -n "$INSTANCE_DIR/$name.lock" \
            flock -w "${COOP_CRON_LOCK_TIMEOUT:-600}" "$COOP_CRON_LOCK" \
            "$PYTHON" "$SCRIPT_DIR/$name.py" "$@" >> "$log_file" 2>&1
    fi
    exec flock -n "$INSTANCE_DIR/$name.lock" "$PYTHON" "$SCRIPT_DIR/$name.py" "$@" >> "$log_file" 2>&1
}
