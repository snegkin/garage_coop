#!/usr/bin/env bash
# Выкатка сайта на сервер — запускается на своём компьютере, не на сервере.
#
#   deployment/deploy-from-git/deploy.sh [-c конфиг] [-y] [--force]   обновить сайт
#   deployment/deploy-from-git/deploy.sh [-c конфиг] maintenance on|off|status
#
# Настройки (ветка, сервер, логин, пароль, каталог, сервисы) — в конфиге,
# который ищется так: -c ПУТЬ → DEPLOY_CONFIG → deploy.conf рядом со
# скриптом (в .gitignore) → ~/.config/garage-coop/deploy.conf.
# Образец — deployment/deploy-from-git/deploy.conf.example.
#
# Что делает обновление (всё — на сервере, одним SSH-сеансом):
#   1. git fetch ветки; локальные правки в коде на сервере — отказ;
#   2. включает заглушку «Ведутся технические работы» (файл-флаг
#      deployment/deploy-from-git/maintenance.on, его проверяет nginx —
#      deployment/deploy-from-git/nginx-maintenance.conf);
#   3. бэкап БД (scripts/backup_db.sh) — перед миграциями;
#   4. git merge --ff-only, pip install (если менялся requirements.txt),
#      scripts/vendor.sh verify;
#   5. systemctl restart сервисов (миграции накатываются при старте) и
#      ожидание, пока каждый начнёт отвечать без 5xx;
#   6. снимает заглушку.
# Ошибка до перезапуска — код возвращается на прежний коммит, заглушка
# снимается: работает прежняя версия, ничего не изменилось. Ошибка при
# перезапуске/проверке — заглушка ОСТАЁТСЯ (миграции могли пройти, откат
# кода без отката БД опаснее), разбираться руками; снять — maintenance off.
#
# Пароль не попадает ни в командную строку (sshpass -e берёт его из
# окружения), ни на диск сервера: удалённый скрипт уходит через stdin SSH.

set -euo pipefail

# Не из консоли (двойной щелчок) — открыть терминал и перезапуститься в нём;
# по завершении окно ждёт Enter. Подтверждение выкатки там же спросится.
# shellcheck source=../in_terminal.sh
. "$(dirname "$0")/../in_terminal.sh"
in_terminal "Обновление сайта кооператива" "$0" "$@"

LOCAL_CONFIG="$(dirname "$0")/deploy.conf"
if [ -n "${DEPLOY_CONFIG:-}" ]; then
    CONFIG="$DEPLOY_CONFIG"
elif [ -f "$LOCAL_CONFIG" ]; then
    CONFIG="$LOCAL_CONFIG"
else
    CONFIG="$HOME/.config/garage-coop/deploy.conf"
fi
ASSUME_YES=0
FORCE=0
ACTION=deploy

usage() {
    sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -c|--config) CONFIG="${2:?после -c нужен путь к конфигу}"; shift 2 ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage 0 ;;
        maintenance)
            case "${2:-}" in
                on|off|status) ACTION="maintenance-$2"; shift 2 ;;
                *) echo "maintenance: нужно on, off или status" >&2; exit 2 ;;
            esac ;;
        *) echo "Неизвестный аргумент: $1" >&2; usage 2 ;;
    esac
done

if [ ! -f "$CONFIG" ]; then
    echo "Нет конфига деплоя: $CONFIG" >&2
    echo "Образец: $(dirname "$0")/deploy.conf.example — скопируйте в $LOCAL_CONFIG" >&2
    echo "или ~/.config/garage-coop/deploy.conf и chmod 600" >&2
    exit 1
fi
echo "Конфиг: $CONFIG" >&2
# Конфиг с паролем, доступный на чтение другим, — предупреждаем.
if [ -n "$(find "$CONFIG" -perm /044 2>/dev/null)" ]; then
    echo "ВНИМАНИЕ: $CONFIG читается другими пользователями — chmod 600 \"$CONFIG\"" >&2
fi

DEPLOY_BRANCH=master
DEPLOY_PORT=22
DEPLOY_PASSWORD=
DEPLOY_SSH_KEY=
DEPLOY_SSH_OPTS=
APP_USER=
SERVICES=garage-coop
HEALTH_URLS=http://127.0.0.1:8000/
HEALTH_TIMEOUT=90
BACKUP_BEFORE=1
BACKUP_ENV_FILES=
# shellcheck source=deploy.conf.example
. "$CONFIG"

: "${DEPLOY_HOST:?в конфиге не задан DEPLOY_HOST}"
: "${DEPLOY_USER:?в конфиге не задан DEPLOY_USER}"
: "${APP_DIR:?в конфиге не задан APP_DIR}"

SSH_OPTS=(-p "$DEPLOY_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
[ -n "$DEPLOY_SSH_KEY" ] && SSH_OPTS+=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes)
# shellcheck disable=SC2206  # намеренно: строка опций разбивается по пробелам
[ -n "$DEPLOY_SSH_OPTS" ] && SSH_OPTS+=($DEPLOY_SSH_OPTS)
if [ -n "$DEPLOY_PASSWORD" ]; then
    command -v sshpass >/dev/null || { echo "Для входа по паролю нужен sshpass (apt install sshpass)" >&2; exit 1; }
    export SSHPASS="$DEPLOY_PASSWORD"
    SSH=(sshpass -e ssh "${SSH_OPTS[@]}")
else
    SSH=(ssh "${SSH_OPTS[@]}" -o BatchMode=yes)
fi

if [ "$ACTION" = deploy ]; then
    # Локальные коммиты, которых нет в origin, на сервер не попадут — git
    # там берёт ветку из origin. Сами мы не пушим: только предупреждаем.
    REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
    if git -C "$REPO_DIR" rev-parse --verify -q "refs/heads/$DEPLOY_BRANCH" >/dev/null \
        && git -C "$REPO_DIR" rev-parse --verify -q "refs/remotes/origin/$DEPLOY_BRANCH" >/dev/null; then
        AHEAD=$(git -C "$REPO_DIR" rev-list --count "origin/$DEPLOY_BRANCH..$DEPLOY_BRANCH")
        if [ "$AHEAD" -gt 0 ]; then
            echo "ВНИМАНИЕ: в локальной $DEPLOY_BRANCH $AHEAD коммит(ов) не запушено — на сервер они не попадут." >&2
        fi
    fi
    if [ "$ASSUME_YES" != 1 ]; then
        read -r -p "Выкатить ветку $DEPLOY_BRANCH на $DEPLOY_USER@$DEPLOY_HOST:$APP_DIR? [y/N] " answer
        case "$answer" in y|Y|д|Д|yes|да) ;; *) echo "Отменено."; exit 1 ;; esac
    fi
fi

# Значения подставляются в начало удалённого скрипта через printf %q —
# никакое содержимое (пробелы, кавычки в пароле) не ломает его разбор.
remote_vars() {
    local name
    for name in ACTION FORCE DEPLOY_BRANCH DEPLOY_PASSWORD APP_DIR APP_USER SERVICES \
                HEALTH_URLS HEALTH_TIMEOUT BACKUP_BEFORE BACKUP_ENV_FILES; do
        printf '%s=%q\n' "$name" "${!name}"
    done
}

# Тело целиком в { … } </dev/null: bash -s читает скрипт из stdin, и любая
# команда, читающая stdin (ssh внутри git fetch и т.п.), иначе съела бы
# остаток скрипта. Группу bash разбирает полностью до выполнения.
{ remote_vars; cat <<'REMOTE'
set -Eeuo pipefail
{

log()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

FLAG="$APP_DIR/deployment/deploy-from-git/maintenance.on"

sudo_cmd() {
    if [ -n "$DEPLOY_PASSWORD" ]; then
        printf '%s\n' "$DEPLOY_PASSWORD" | sudo -S -p '' "$@"
    else
        sudo -n "$@"
    fi
}
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo_cmd "$@"; fi; }
# Код принадлежит пользователю сайта: git под чужим пользователем ругается
# на «dubious ownership», а файлы, созданные не им, сайт потом не перезапишет.
as_app() {
    if [ -z "$APP_USER" ] || [ "$APP_USER" = "$(id -un)" ]; then
        "$@"
    else
        sudo_cmd -u "$APP_USER" -H "$@"
    fi
}
git_app() { as_app git -C "$APP_DIR" "$@"; }

# mkdir — на сервере может стоять код старше, чем каталог deployment/deploy-from-git/.
maintenance_on()  { as_app mkdir -p "${FLAG%/*}"; as_app touch "$FLAG"; }
maintenance_off() { as_app rm -f "$FLAG"; }

case "$ACTION" in
    maintenance-on)  maintenance_on;  log "Заглушка включена";  exit 0 ;;
    maintenance-off) maintenance_off; log "Заглушка выключена"; exit 0 ;;
    maintenance-status)
        if [ -f "$FLAG" ]; then echo "Заглушка включена ($FLAG)"; else echo "Заглушка выключена"; fi
        exit 0 ;;
esac

[ -d "$APP_DIR/.git" ] || { echo "Нет git-репозитория в $APP_DIR" >&2; exit 1; }
cd "$APP_DIR"

# stage: pre — ничего не менялось; code — код обновлён, сервис ещё на
# старом; restart — сервис перезапущен на новом коде (миграции).
STAGE=pre
MAINT_WAS_ON=0
[ -f "$FLAG" ] && MAINT_WAS_ON=1

on_error() {
    local rc=$?
    trap - ERR
    set +e
    case "$STAGE" in
        pre)
            [ "$MAINT_WAS_ON" = 1 ] || maintenance_off
            warn "Деплой прерван, на сервере ничего не изменилось." ;;
        code)
            warn "Ошибка до перезапуска — возвращаю код на $OLD_HEAD."
            if [ -n "$OLD_BRANCH" ] && [ "$OLD_BRANCH" != "$DEPLOY_BRANCH" ]; then
                git_app checkout -q "$OLD_BRANCH"
            elif [ -n "$OLD_BRANCH" ]; then
                git_app reset -q --keep "$OLD_HEAD"
            else
                git_app checkout -q "$OLD_HEAD"
            fi
            [ "$MAINT_WAS_ON" = 1 ] || maintenance_off
            warn "Работает прежняя версия, заглушка снята." ;;
        restart)
            warn "Ошибка при перезапуске/проверке сервиса. Заглушка ОСТАВЛЕНА включённой."
            warn "Смотрите: sudo journalctl -u <сервис> -n 100; логи — instance/logs/app-*.log"
            warn "Снять заглушку: deployment/deploy-from-git/deploy.sh maintenance off" ;;
    esac
    exit "$rc"
}
trap on_error ERR

log "Получаю $DEPLOY_BRANCH из origin"
git_app fetch -q --prune origin "$DEPLOY_BRANCH"

if [ -n "$(git_app status --porcelain --untracked-files=no)" ]; then
    git_app status --short --untracked-files=no >&2
    echo "В коде на сервере есть локальные правки — деплой отменён." >&2
    false
fi

OLD_HEAD=$(git_app rev-parse HEAD)
OLD_BRANCH=$(git_app symbolic-ref -q --short HEAD || true)
NEW_HEAD=$(git_app rev-parse "origin/$DEPLOY_BRANCH")

if [ "$OLD_HEAD" = "$NEW_HEAD" ] && [ "$OLD_BRANCH" = "$DEPLOY_BRANCH" ] && [ "$FORCE" != 1 ]; then
    log "Уже на $(git_app log -1 --format='%h %s'), обновлять нечего (--force — перезапустить всё равно)"
    exit 0
fi

echo "Было:  $(git_app log -1 --format='%h %s' "$OLD_HEAD")"
echo "Будет: $(git_app log -1 --format='%h %s' "$NEW_HEAD")"
if git_app merge-base --is-ancestor "$OLD_HEAD" "$NEW_HEAD"; then
    git_app log -n 30 --format='  %h %s' "$OLD_HEAD..$NEW_HEAD"
fi

log "Включаю заглушку «Технические работы»"
maintenance_on

if [ "$BACKUP_BEFORE" = 1 ]; then
    log "Бэкап БД"
    if [ -z "$BACKUP_ENV_FILES" ]; then
        as_app "$APP_DIR/scripts/backup_db.sh"
    else
        for env_file in $BACKUP_ENV_FILES; do
            as_app env ENV_FILE="$env_file" "$APP_DIR/scripts/backup_db.sh"
        done
    fi
fi

STAGE=code
log "Обновляю код"
if [ "$OLD_BRANCH" != "$DEPLOY_BRANCH" ]; then
    git_app checkout -q "$DEPLOY_BRANCH"
fi
git_app merge -q --ff-only "origin/$DEPLOY_BRANCH"

if ! git_app diff --quiet "$OLD_HEAD" HEAD -- requirements.txt; then
    VENV_PIP=""
    for p in "$APP_DIR/venv/bin/pip" "$APP_DIR/.venv/bin/pip"; do
        [ -x "$p" ] && { VENV_PIP=$p; break; }
    done
    [ -n "$VENV_PIP" ] || { echo "Не найден venv в $APP_DIR" >&2; false; }
    log "requirements.txt изменился — pip install"
    as_app "$VENV_PIP" install -q -r "$APP_DIR/requirements.txt"
fi

log "Проверяю библиотеки фронтенда"
as_app "$APP_DIR/scripts/vendor.sh" verify

STAGE=restart
for svc in $SERVICES; do
    log "Перезапускаю $svc"
    as_root systemctl restart "$svc"
done

if command -v curl >/dev/null; then
    for url in $HEALTH_URLS; do
        log "Жду ответа $url"
        deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
        while :; do
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || true)
            if [ "$code" != 000 ] && [ "$code" -lt 500 ]; then
                echo "  HTTP $code"
                break
            fi
            if [ "$(date +%s)" -ge "$deadline" ]; then
                echo "  $url не ответил за ${HEALTH_TIMEOUT} с (последний код: $code)" >&2
                false
            fi
            sleep 2
        done
    done
else
    warn "curl не установлен — проверку ответа сайта пропускаю"
fi

maintenance_off
log "Готово: $(git_app log -1 --format='%h %s'), заглушка снята"
} </dev/null
REMOTE
} | "${SSH[@]}" "$DEPLOY_USER@$DEPLOY_HOST" 'bash -s'
