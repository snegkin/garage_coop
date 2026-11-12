#!/usr/bin/env python3
"""
Внешний бэкап: данные кооператива уходят с сервера. Запускается по cron
(scripts/backup_offsite.sh) после backup_db и backup_mailbox, см.
docs/deployment.md, «Внешний бэкап».

scripts/backup_db.py кладёт снимки на тот же диск, что и сама БД, поэтому
со смертью сервера пропадают и они. Этот скрипт делает две вещи:

1. Каталог для забора, всегда: INSTANCE_DIR/backup_staging/. Внутри
   консистентный снимок БД (coop.db, через sqlite backup API, как в
   backup_db.py), копия файла окружения (env: SECRET_KEY/
   BANK_API_ENCRYPTION_KEY, без них сохранённые в БД пароли и токены не
   расшифровать) и MANIFEST.json. Оттуда, вместе с uploads/, bank_certs/,
   bundles/ и mail_backup/, данные забирает агент на компьютере
   председателя (agents/backup/, rsync по ssh только на чтение). Каждый
   файл подменяется атомарно, так что rsync посреди обновления не увидит
   недописанного.

2. restic-репозиторий, если задан RESTIC_REPOSITORY (Яндекс.Диск через
   rclone: rclone:yadisk:coop-backup; или S3 — Yandex Object Storage).
   restic шифрует на сервере, хранит историю версий и передаёт только
   изменения: снимок БД кладётся несжатым, чтобы между днями
   дедуплицировались неизменившиеся страницы SQLite (restic сжимает сам).
   Пароль репозитория — RESTIC_PASSWORD или RESTIC_PASSWORD_FILE (для S3
   ещё AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY); restic читает их из
   окружения сам, rclone — свой ~/.config/rclone/rclone.conf. Пароль restic обязательно храните ещё где-то вне сервера:
   без него репозиторий не расшифровать.

Ротация в репозитории: OFFSITE_KEEP_DAILY/WEEKLY/MONTHLY (14/8/12);
OFFSITE_PRUNE=0 отключает forget/prune (если ключ S3 выдан без права
удаления). По воскресеньям — restic check.

Итог каждого запуска — logs/backup_offsite_status.json (время, успех,
id снимка), по нему видно, что бэкап не умер молча.

Запуск вручную:
    cd /path/to/project && python3 scripts/backup_offsite.py
"""
import datetime as dt
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import INSTANCE_DIR, db_path  # noqa: E402

STAGING_NAME = "backup_staging"
# Каталоги INSTANCE_DIR, которые уходят в бэкап, если существуют. dvr/ нет
# умышленно: кадры с камер — поток, а не данные кооператива. Переопределённые
# в .env UPLOAD_FOLDER/BANK_CERTS_FOLDER вне INSTANCE_DIR сюда не попадут.
DATA_DIRS = ("uploads", "bank_certs", "bundles", "mail_backup")
RESTIC_TIMEOUT = 6 * 3600


def log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


class BackupError(Exception):
    pass


def staging_dir() -> str:
    return os.environ.get("BACKUP_STAGING_DIR") or os.path.join(INSTANCE_DIR, STAGING_NAME)


def _snapshot_db(source_path: str, target_path: str) -> str | None:
    """Снимок через backup API во временный файл → quick_check → атомарная
    подмена. Возвращает ревизию alembic (для MANIFEST), если есть."""
    tmp_path = target_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    source = sqlite3.connect(source_path)
    dest = sqlite3.connect(tmp_path)
    try:
        source.backup(dest)
        status = dest.execute("PRAGMA quick_check").fetchone()[0]
        if status != "ok":
            raise BackupError(f"снимок БД не прошёл quick_check: {status}")
        try:
            row = dest.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            row = None
    finally:
        dest.close()
        source.close()
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, target_path)
    return row[0] if row else None


def _copy_private(source: str, target: str) -> None:
    tmp_path = target + ".tmp"
    shutil.copyfile(source, tmp_path)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, target)


def stage() -> str:
    """Готовит staging-каталог; возвращает его путь."""
    source_db = db_path()
    if not os.path.exists(source_db):
        raise BackupError(f"БД не найдена: {source_db}")
    target = staging_dir()
    os.makedirs(target, exist_ok=True)
    os.chmod(target, 0o700)

    revision = _snapshot_db(source_db, os.path.join(target, "coop.db"))

    env_file = os.environ.get("ENV_FILE")
    env_copied = False
    if env_file and os.path.isfile(env_file):
        _copy_private(env_file, os.path.join(target, "env"))
        env_copied = True
    else:
        log("файл окружения не найден (ENV_FILE) — в бэкап не попадёт; "
            "без SECRET_KEY/BANK_API_ENCRYPTION_KEY сохранённые секреты не расшифровать")

    manifest = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "db_revision": revision,
        "env_included": env_copied,
        "data_dirs": [name for name in DATA_DIRS if os.path.isdir(os.path.join(INSTANCE_DIR, name))],
    }
    manifest_path = os.path.join(target, "MANIFEST.json")
    with open(manifest_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(manifest_path + ".tmp", manifest_path)
    return target


def backup_paths(staging: str) -> list[str]:
    """Пути относительно INSTANCE_DIR — restic запускается из него, и в
    снимке они такими и останутся (restore в любой каталог без
    абсолютных путей сервера)."""
    paths = [os.path.relpath(staging, INSTANCE_DIR)]
    paths += [name for name in DATA_DIRS if os.path.isdir(os.path.join(INSTANCE_DIR, name))]
    return paths


def _restic(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    binary = os.environ.get("RESTIC_BIN", "restic")
    try:
        result = subprocess.run(
            [binary, *args], cwd=INSTANCE_DIR, capture_output=True, text=True, timeout=RESTIC_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"не найден restic ({binary}) — apt install restic") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"restic {args[0]}: не уложился в {RESTIC_TIMEOUT} с") from exc
    if check and result.returncode != 0:
        raise BackupError(f"restic {args[0]} (код {result.returncode}): {result.stderr.strip()[-2000:]}")
    return result


def ensure_repository() -> None:
    result = _restic(["cat", "config"], check=False)
    if result.returncode == 0:
        return
    # 10 — «репозитория нет» (restic ≥ 0.17); иное — сеть, пароль, ключи.
    if result.returncode != 10:
        raise BackupError(f"нет доступа к репозиторию (код {result.returncode}): {result.stderr.strip()[-2000:]}")
    log("репозитория ещё нет — restic init")
    _restic(["init"])


def _host() -> str:
    return os.environ.get("OFFSITE_BACKUP_HOST") or socket.gethostname()


def run_restic(paths: list[str]) -> str | None:
    ensure_repository()
    result = _restic(["backup", "--json", "--host", _host(), "--tag", "coop", "--", *paths])
    snapshot_id = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("message_type") == "summary":
            snapshot_id = event.get("snapshot_id")
            added = event.get("data_added", 0)
            log(f"снимок {snapshot_id}: файлов {event.get('total_files_processed')}, "
                f"новых данных {added / 1024 / 1024:.1f} МБ")

    if os.environ.get("OFFSITE_PRUNE", "1") != "0":
        _restic([
            "forget", "--host", _host(), "--tag", "coop", "--prune",
            "--keep-daily", os.environ.get("OFFSITE_KEEP_DAILY", "14"),
            "--keep-weekly", os.environ.get("OFFSITE_KEEP_WEEKLY", "8"),
            "--keep-monthly", os.environ.get("OFFSITE_KEEP_MONTHLY", "12"),
        ])
    if dt.date.today().weekday() == 6:
        _restic(["check"])
        log("restic check: ok")
    return snapshot_id


def write_status(status: dict) -> None:
    log_dir = os.path.join(INSTANCE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "backup_offsite_status.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


def main() -> int:
    status = {"finished_at": None, "ok": False, "staged": False, "restic": None, "snapshot_id": None, "error": None}
    try:
        staging = stage()
        status["staged"] = True
        log(f"каталог для забора обновлён: {staging}")
        if os.environ.get("RESTIC_REPOSITORY"):
            status["restic"] = True
            status["snapshot_id"] = run_restic(backup_paths(staging))
        else:
            status["restic"] = False
            log("RESTIC_REPOSITORY не задан — внешний репозиторий не настроен, только каталог для забора")
        status["ok"] = True
    except BackupError as exc:
        status["error"] = str(exc)
        log(f"ОШИБКА: {exc}")
    status["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_status(status)
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
