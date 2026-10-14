#!/bin/sh
# Обёртка для scripts/vendor.py — библиотеки фронтенда (app/static/vendor/,
# см. README там же). Ручная операция, не cron — как restore_db.sh: без
# flock и без лога, вывод прямо в терминал. Окружение (.env) скрипту не
# нужно — он работает только с файлами проекта и реестром npm, — но общая
# часть обёрток подгружает его заодно с python из venv, это не мешает.
#
# Примеры:
#   ./scripts/vendor.sh verify               # без сети: файлы совпадают с манифестом
#   ./scripts/vendor.sh install              # восстановить недостающее/испорченное тех же версий
#   ./scripts/vendor.sh check                # устаревшие версии и известные уязвимости
#   ./scripts/vendor.sh update [пакет ...]   # обновить (--major, --to ВЕРСИЯ)

set -eu

. "$(dirname "$0")/_common.sh"

exec "$PYTHON" "$SCRIPT_DIR/vendor.py" "$@"
