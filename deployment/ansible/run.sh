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
#
# Настройки — как в README.md: ~/.config/garage-coop/{inventory.ini,vars.yml};
# если их там нет, берутся inventory.ini/vars.yml рядом со скриптом.
# COOP_VARS и -i в аргументах имеют приоритет.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "$0")"

# --- не в консоли → открыть терминал и перезапуститься в нём -------------

if [ -z "${COOP_RUN_IN_TERMINAL:-}" ] && ! [ -t 0 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    export COOP_RUN_IN_TERMINAL=1
    TITLE="Развёртывание сервера кооператива"
    for term in x-terminal-emulator gnome-terminal konsole xfce4-terminal mate-terminal kitty alacritty xterm; do
        command -v "$term" >/dev/null 2>&1 || continue
        case "$term" in
            gnome-terminal) exec gnome-terminal --title="$TITLE" -- "$SELF" "$@" ;;
            xfce4-terminal|mate-terminal) exec "$term" --title="$TITLE" -x "$SELF" "$@" ;;
            kitty) exec kitty --title "$TITLE" "$SELF" "$@" ;;
            xterm) exec xterm -T "$TITLE" -e "$SELF" "$@" ;;
            *) exec "$term" -e "$SELF" "$@" ;;
        esac
    done
    MSG="Не найден эмулятор терминала. Запустите в консоли: $SELF"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --text="$MSG" 2>/dev/null
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "$TITLE" "$MSG"
    fi
    echo "$MSG" >&2
    exit 1
fi

# Окно, открытое нами, не закрывается сразу — даже при ошибке.
pause() {
    if [ -n "${COOP_RUN_IN_TERMINAL:-}" ]; then
        printf '\nНажмите Enter, чтобы закрыть окно… '
        read -r _ || true
    fi
}

cd "$SCRIPT_DIR" || { pause; exit 1; }

if ! command -v ansible-playbook >/dev/null 2>&1; then
    echo "Не найден ansible-playbook. Установка (README.md):" >&2
    echo "  sudo apt install ansible-core && ansible-galaxy collection install ansible.posix community.general" >&2
    pause
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

echo "Плейбук: $SCRIPT_DIR/site.yml"
echo "Настройки: ${COOP_VARS:-$CONF_DIR/vars.yml}"
echo "Инвентарь: ${INVENTORY:-по ansible.cfg ($CONF_DIR/inventory.ini) или из аргументов}"
echo

if [ -n "$INVENTORY" ]; then
    ansible-playbook -i "$INVENTORY" site.yml "$@"
else
    ansible-playbook site.yml "$@"
fi
rc=$?

echo
if [ "$rc" -eq 0 ]; then
    echo "Готово."
else
    echo "Плейбук завершился с ошибкой (код $rc)."
fi
pause
exit "$rc"
