#!/bin/sh
# Запуск плейбука site.yml «двойным щелчком»: если скрипт запущен не из
# консоли (файловый менеджер, ярлык), он открывает окно терминала и
# выполняется в нём. Плейбуку нужна консоль: при первом прогоне он
# печатает deploy key и ждёт Enter. По окончании окно ждёт Enter, чтобы
# успеть прочитать итог.
#
#   ./run.sh                    полный прогон
#   ./run.sh --tags app         аргументы передаются в ansible-playbook
#   ./run.sh --check --diff
#   ./run.sh --tags local       только агенты на этом компьютере (бэкап, ЭЗП)
#
# Настройки — как в README.md: ~/.config/garage-coop/{inventory.ini,vars.yml};
# если их там нет, берутся inventory.ini/vars.yml рядом со скриптом.
# COOP_VARS и -i в аргументах имеют приоритет.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Не из консоли (двойной щелчок) — открыть терминал и перезапуститься в нём;
# по завершении окно ждёт Enter.
. "$SCRIPT_DIR/../in_terminal.sh"
in_terminal "Развёртывание сервера кооператива" "$0" "$@"

cd "$SCRIPT_DIR" || exit 1

if ! command -v ansible-playbook >/dev/null 2>&1; then
    echo "Не найден ansible-playbook. Установка (README.md):" >&2
    echo "  sudo apt install ansible-core && ansible-galaxy collection install ansible.posix community.general" >&2
    exit 1
fi

CONF_DIR="$HOME/.config/garage-coop"
if [ -z "${COOP_VARS:-}" ] && [ ! -f "$CONF_DIR/vars.yml" ] && [ -f "$SCRIPT_DIR/vars.yml" ]; then
    export COOP_VARS="$SCRIPT_DIR/vars.yml"
fi

INVENTORY=""
case " $* " in
    *" -i"*|*" --inventory"*) ;;
    *)
        if [ ! -f "$CONF_DIR/inventory.ini" ] && [ -f "$SCRIPT_DIR/inventory.ini" ]; then
            INVENTORY="$SCRIPT_DIR/inventory.ini"
        fi
        ;;
esac

# Агенты на этом компьютере (local_backup_agent / local_ezp_agent) ставятся
# через sudo — спросить пароль, если sudo без него не пускает. Спрашиваем
# сами, а не через -K: у ansible приглашение «BECOME password:». Пароль
# сразу проверяется через sudo и передаётся в ansible файлом (0600, удаляется
# по выходу).
VARS_FILE="${COOP_VARS:-$CONF_DIR/vars.yml}"
BECOME=""
PASS_FILE=""
cleanup() { if [ -n "$PASS_FILE" ]; then rm -f "$PASS_FILE"; fi; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM HUP

if [ -f "$VARS_FILE" ] && grep -Eq '^local_(backup|ezp)_agent:[[:space:]]*(true|yes|True)' "$VARS_FILE"; then
    case " $* " in
        *" -K "*|*" --ask-become-pass "*|*" --become-password-file"*|*" --become-pass-file"*) ;;
        *)
            if ! sudo -n true 2>/dev/null; then
                PASS_FILE="$(umask 077 && mktemp)" || exit 1
                tries=0
                while :; do
                    printf 'Пароль sudo пользователя %s на этом компьютере (для агентов бэкапа и ЭЗП): ' "$(id -un)"
                    stty -echo 2>/dev/null
                    IFS= read -r pw || pw=""
                    stty echo 2>/dev/null
                    echo
                    printf '%s\n' "$pw" > "$PASS_FILE"
                    if sudo -S -k -v -p '' < "$PASS_FILE" 2>/dev/null; then
                        break
                    fi
                    tries=$((tries + 1))
                    if [ "$tries" -ge 3 ]; then
                        echo "Неверный пароль sudo — три попытки, выход." >&2
                        exit 1
                    fi
                    echo "Неверный пароль, ещё раз." >&2
                done
                unset pw
                BECOME="--become-password-file $PASS_FILE"
            fi
            ;;
    esac
fi

echo "Плейбук: $SCRIPT_DIR/site.yml"
echo "Настройки: ${COOP_VARS:-$CONF_DIR/vars.yml}"
echo "Инвентарь: ${INVENTORY:-по ansible.cfg ($CONF_DIR/inventory.ini) или из аргументов}"
echo

if [ -n "$INVENTORY" ]; then
    ansible-playbook -i "$INVENTORY" $BECOME site.yml "$@"
else
    ansible-playbook $BECOME site.yml "$@"
fi
