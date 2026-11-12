#!/bin/sh
# Настройка сервера и агентов — плейбук ansible/site.yml. Можно запускать
# «двойным щелчком»: если скрипт запущен не из
# консоли (файловый менеджер, ярлык), он открывает окно терминала и
# выполняется в нём. Плейбуку нужна консоль: при первом прогоне он
# печатает deploy key и ждёт Enter. По окончании окно ждёт Enter, чтобы
# успеть прочитать итог.
#
#   ./setup.sh                    полный прогон
#   ./setup.sh --tags app         аргументы передаются в ansible-playbook
#   ./setup.sh --check --diff
#   ./setup.sh --tags local       только агенты на этом компьютере (бэкап, ЭЗП)
#
# Настройки — как в ansible/README.md: ~/.config/garage-coop/{inventory.ini,vars.yml};
# если их там нет, берутся ansible/inventory.ini и ansible/vars.yml.
# COOP_VARS и -i в аргументах имеют приоритет.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Плейбук, ansible.cfg и локальные настройки — в ansible/ рядом со скриптом.
ANSIBLE_DIR="$SCRIPT_DIR/ansible"

# Не из консоли (двойной щелчок) — открыть терминал и перезапуститься в нём;
# по завершении окно ждёт Enter.
. "$SCRIPT_DIR/in_terminal.sh"
in_terminal "Развёртывание сервера кооператива" "$0" "$@"

cd "$ANSIBLE_DIR" || exit 1

if ! command -v ansible-playbook >/dev/null 2>&1; then
    echo "Не найден ansible-playbook. Установка (ansible/README.md):" >&2
    echo "  sudo apt install ansible-core && ansible-galaxy collection install ansible.posix community.general" >&2
    exit 1
fi

CONF_DIR="$HOME/.config/garage-coop"
if [ -z "${COOP_VARS:-}" ] && [ ! -f "$CONF_DIR/vars.yml" ] && [ -f "$ANSIBLE_DIR/vars.yml" ]; then
    export COOP_VARS="$ANSIBLE_DIR/vars.yml"
fi

INVENTORY=""
case " $* " in
    *" -i"*|*" --inventory"*) ;;
    *)
        if [ ! -f "$CONF_DIR/inventory.ini" ] && [ -f "$ANSIBLE_DIR/inventory.ini" ]; then
            INVENTORY="$ANSIBLE_DIR/inventory.ini"
        fi
        ;;
esac

# Агенты на этом компьютере (local_backup_agent / local_ezp_agent) ставятся
# через sudo — спросить пароль, если sudo без него не пускает и этот play
# вообще будет выполняться (см. local_play_runs). Спрашиваем
# сами, а не через -K: у ansible приглашение «BECOME password:». Пароль
# сразу проверяется через sudo и передаётся в ansible файлом (0600, удаляется
# по выходу).
VARS_FILE="${COOP_VARS:-$CONF_DIR/vars.yml}"
BECOME=""
PASS_FILE=""
cleanup() { if [ -n "$PASS_FILE" ]; then rm -f "$PASS_FILE"; fi; }
in_terminal_on_exit cleanup
trap 'exit 130' INT TERM HUP

# Выполнится ли вообще play «Агенты на этом компьютере»: все его задачи —
# с тегом local, поэтому при --tags app / --tags nginx и т.п. (без local/all)
# или --skip-tags local он пустой и sudo здесь не нужен — пароль не спрашиваем.
# Так же для режимов, которые ничего не выполняют (--list-tasks и т.п.).
local_play_runs() {
    tags="" skip="" prev=""
    for arg in "$@"; do
        case "$prev" in
            --tags|-t) tags="$tags,$arg" ;;
            --skip-tags) skip="$skip,$arg" ;;
        esac
        case "$arg" in
            --tags=*) tags="$tags,${arg#--tags=}" ;;
            --skip-tags=*) skip="$skip,${arg#--skip-tags=}" ;;
            --syntax-check|--list-tasks|--list-tags|--list-hosts) return 1 ;;
        esac
        prev="$arg"
    done
    case ",$skip," in *,local,*|*,all,*) return 1 ;; esac
    [ -z "$tags" ] && return 0
    case ",$tags," in *,local,*|*,all,*) return 0 ;; esac
    return 1
}

if [ -f "$VARS_FILE" ] && grep -Eq '^local_(backup|ezp)_agent:[[:space:]]*(true|yes|True)' "$VARS_FILE" \
        && local_play_runs "$@"; then
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

echo "Плейбук: $ANSIBLE_DIR/site.yml"
echo "Настройки: ${COOP_VARS:-$CONF_DIR/vars.yml}"
echo "Инвентарь: ${INVENTORY:-по ansible.cfg ($CONF_DIR/inventory.ini) или из аргументов}"
echo

if [ -n "$INVENTORY" ]; then
    ansible-playbook -i "$INVENTORY" $BECOME site.yml "$@"
else
    ansible-playbook $BECOME site.yml "$@"
fi
