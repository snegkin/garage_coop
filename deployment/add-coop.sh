#!/bin/sh
# Ещё один кооператив на том же сервере: спрашивает имя, домен и порт,
# генерирует ключи, дописывает его в coop_extra_instances в vars.yml и
# предлагает сразу прогнать плейбук (--tags instances): пользователь, БД,
# служба, vhost и сертификат, cron, строки в deploy.conf. Можно запускать
# двойным щелчком, как setup.sh.
#
#   ./add-coop.sh
#
# vars.yml и inventory.ini ищутся так же, как в setup.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ANSIBLE_DIR="$SCRIPT_DIR/ansible"

. "$SCRIPT_DIR/in_terminal.sh"
in_terminal "Новый кооператив на сервере" "$0" "$@"

CONF_DIR="$HOME/.config/garage-coop"
VARS_FILE="${COOP_VARS:-$CONF_DIR/vars.yml}"
if [ -z "${COOP_VARS:-}" ] && [ ! -f "$VARS_FILE" ] && [ -f "$ANSIBLE_DIR/vars.yml" ]; then
    VARS_FILE="$ANSIBLE_DIR/vars.yml"
fi
INVENTORY="$CONF_DIR/inventory.ini"
[ -f "$INVENTORY" ] || INVENTORY="$ANSIBLE_DIR/inventory.ini"

if ! python3 -c "import yaml" 2>/dev/null; then
    echo "Нужен PyYAML: sudo apt install python3-yaml (ставится вместе с ansible-core)." >&2
    exit 1
fi

python3 -I "$SCRIPT_DIR/coop_vars.py" add "$VARS_FILE" "$ANSIBLE_DIR/defaults.yml" "$INVENTORY" || exit 1

echo
printf 'Настроить сервер сейчас (setup.sh --tags instances)? (д/н) [д]: '
read -r answer || answer="н"
case "${answer:-д}" in
    д|Д|да|y|Y|yes) ;;
    *)
        echo "Позже: $SCRIPT_DIR/setup.sh --tags instances"
        exit 0
        ;;
esac

# Окно с ожиданием Enter уже наше — setup.sh второй раз не ждёт.
env -u COOP_RUN_IN_TERMINAL COOP_VARS="$VARS_FILE" "$SCRIPT_DIR/setup.sh" --tags instances
