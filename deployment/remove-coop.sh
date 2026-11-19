#!/bin/sh
# Убрать дополнительный кооператив с сервера: показывает список из vars.yml,
# спрашивает, какой убрать и оставить ли данные на сервере, просит
# подтвердить вводом имени. Затем запускает ansible/remove_coop.yml
# (служба, cron, vhost, сертификат, sudo; данные в архив и копия на этот
# компьютер, потом удаляется пользователь) и после успеха убирает
# кооператив из coop_extra_instances в vars.yml. Можно запускать двойным
# щелчком, как setup.sh.
#
#   ./remove-coop.sh
#   ./remove-coop.sh coop2

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ANSIBLE_DIR="$SCRIPT_DIR/ansible"

. "$SCRIPT_DIR/in_terminal.sh"
in_terminal "Убрать кооператив с сервера" "$0" "$@"

CONF_DIR="$HOME/.config/garage-coop"
VARS_FILE="${COOP_VARS:-$CONF_DIR/vars.yml}"
if [ -z "${COOP_VARS:-}" ] && [ ! -f "$VARS_FILE" ] && [ -f "$ANSIBLE_DIR/vars.yml" ]; then
    VARS_FILE="$ANSIBLE_DIR/vars.yml"
fi

if ! python3 -c "import yaml" 2>/dev/null; then
    echo "Нужен PyYAML: sudo apt install python3-yaml (ставится вместе с ansible-core)." >&2
    exit 1
fi

LIST="$(python3 -I "$SCRIPT_DIR/coop_vars.py" list "$VARS_FILE")" || exit 1
if [ -z "$LIST" ]; then
    echo "В $VARS_FILE нет дополнительных кооперативов (coop_extra_instances)."
    echo "Основной кооператив этим скриптом не удаляется."
    exit 1
fi

echo "Файл настроек: $VARS_FILE"
echo "Дополнительные кооперативы:"
printf '%s\n' "$LIST" | while IFS="$(printf '\t')" read -r n d p; do
    echo "  $n — $d, порт $p"
done
echo

NAME="${1:-}"
if [ -z "$NAME" ]; then
    printf 'Какой убрать (имя): '
    read -r NAME || exit 1
fi
if ! printf '%s\n' "$LIST" | cut -f1 | grep -qxF "$NAME"; then
    echo "Кооператива «$NAME» в списке нет." >&2
    exit 1
fi

echo
echo "Данные (БД, загруженные файлы, coop.env):"
echo "  1 — упаковать в архив (на сервере /var/backups/garage-coop/ и копия"
echo "      в ~/garage-coop-archive/ здесь), затем удалить с сервера"
echo "  2 — оставить на сервере в /home/$NAME (пользователь будет заблокирован)"
printf 'Выбор [1]: '
read -r choice || exit 1
case "${choice:-1}" in
    1) KEEP=false ;;
    2) KEEP=true ;;
    *) echo "Нужно 1 или 2." >&2; exit 1 ;;
esac

echo
echo "Сайт кооператива «$NAME» перестанет работать, служба, cron, vhost и"
echo "сертификат будут удалены."
printf 'Для подтверждения введите имя ещё раз: '
read -r confirm || exit 1
if [ "$confirm" != "$NAME" ]; then
    echo "Не совпало — ничего не сделано."
    exit 1
fi

# Окно с ожиданием Enter уже наше — setup.sh второй раз не ждёт.
if ! env -u COOP_RUN_IN_TERMINAL COOP_VARS="$VARS_FILE" COOP_PLAYBOOK=remove_coop.yml \
        "$SCRIPT_DIR/setup.sh" -e "coop_remove=$NAME" -e "coop_keep_data=$KEEP"; then
    echo >&2
    echo "Плейбук завершился с ошибкой — vars.yml не изменён. Исправьте причину и" >&2
    echo "запустите скрипт снова: уже сделанные шаги повторно ничего не сломают." >&2
    exit 1
fi

echo
python3 -I "$SCRIPT_DIR/coop_vars.py" remove "$VARS_FILE" "$NAME" || exit 1
if [ "$KEEP" = true ]; then
    echo "Ключи кооператива удалены из vars.yml вместе с записью. Чтобы потом вернуть"
    echo "его с этими данными, нужны те же secret_key и bank_api_encryption_key —"
    echo "они есть в /home/$NAME/coop.env на сервере."
fi
