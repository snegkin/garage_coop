#!/bin/sh
# Forced command для ssh-ключа агента локального бэкапа (backup_agent/ на
# компьютере председателя). В ~/.ssh/authorized_keys пользователя сайта:
#
#   command="/path/to/project/scripts/backup_pull_gate.sh",restrict ssh-ed25519 AAAA… coop-backup
#
# Пропускает только rsync на чтение внутри INSTANCE_DIR (rrsync -ro из
# пакета rsync) и отмечает время забора в logs/backup_pull.log: по нему на
# сервере видно, что копия у председателя ещё обновляется.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
# INSTANCE_DIR из .env, если задан, — без запуска остального .env.
if [ -z "${INSTANCE_DIR:-}" ] && [ -f "$ENV_FILE" ]; then
    INSTANCE_DIR="$(sed -n 's/^INSTANCE_DIR=//p' "$ENV_FILE" | tail -n 1)"
fi
INSTANCE_DIR="${INSTANCE_DIR:-$PROJECT_DIR/instance}"

mkdir -p "$INSTANCE_DIR/logs"
echo "$(date -Iseconds) ${SSH_CLIENT%% *} ${SSH_ORIGINAL_COMMAND:-}" >> "$INSTANCE_DIR/logs/backup_pull.log"

RRSYNC="$(command -v rrsync || true)"
[ -n "$RRSYNC" ] || RRSYNC=/usr/share/doc/rsync/scripts/rrsync
exec "$RRSYNC" -ro "$INSTANCE_DIR"
