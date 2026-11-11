# Подключается из скриптов деплоя (ansible/run.sh, deploy-from-git/deploy.sh):
#
#   . "$(dirname "$0")/../in_terminal.sh"
#   in_terminal "Заголовок окна" "$0" "$@"
#
# Если скрипт запущен не из консоли (двойной щелчок в файловом менеджере,
# ярлык), открывает окно терминала и перезапускает скрипт в нём. Такое окно
# по завершении ждёт Enter (и при ошибке тоже), чтобы итог можно было
# прочитать. Из консоли ничего не меняется.

_in_terminal_pause() {
    rc=$?
    echo
    if [ "$rc" -eq 0 ]; then
        echo "Готово."
    else
        echo "Завершилось с ошибкой (код $rc)."
    fi
    printf 'Нажмите Enter, чтобы закрыть окно… '
    read -r _ || true
    exit "$rc"
}

in_terminal() {
    _title="$1"
    _self="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
    shift 2

    if [ -n "${COOP_RUN_IN_TERMINAL:-}" ]; then
        # Уже в окне, которое открыли мы.
        trap _in_terminal_pause EXIT
        return 0
    fi
    # Консоль есть или графики нет — работаем как обычно.
    if [ -t 0 ] || { [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; }; then
        return 0
    fi

    export COOP_RUN_IN_TERMINAL=1
    for _term in x-terminal-emulator ptyxis gnome-terminal konsole xfce4-terminal mate-terminal kitty alacritty xterm; do
        command -v "$_term" >/dev/null 2>&1 || continue
        case "$_term" in
            gnome-terminal) exec gnome-terminal --title="$_title" -- "$_self" "$@" ;;
            ptyxis) exec ptyxis --new-window -- "$_self" "$@" ;;
            xfce4-terminal|mate-terminal) exec "$_term" --title="$_title" -x "$_self" "$@" ;;
            kitty) exec kitty --title "$_title" "$_self" "$@" ;;
            xterm) exec xterm -T "$_title" -e "$_self" "$@" ;;
            *) exec "$_term" -e "$_self" "$@" ;;
        esac
    done

    _msg="Не найден эмулятор терминала. Запустите в консоли: $_self"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --text="$_msg" 2>/dev/null
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "$_title" "$_msg"
    fi
    echo "$_msg" >&2
    exit 1
}
