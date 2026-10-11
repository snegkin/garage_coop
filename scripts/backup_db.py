#!/usr/bin/env python3
"""
Резервное копирование БД — для запуска по cron (см. scripts/backup_db.sh и
docs/deployment.md, раздел «Автоматизация»).

SQLite — это один файл на диске сервера; без отдельного бэкапа потеря диска
(или случайное "rm -rf instance/") = потеря всех данных кооператива без
возможности восстановления. Копирует instance/coop.db через встроенный
sqlite3 backup API (а не простой `cp`) — это безопасно делать даже пока
приложение работает и пишет в БД: backup API берёт консистентный снимок
через тот же WAL-механизм, которым сама SQLite защищает читателей от
писателя (см. app/database.py — WAL включён именно поэтому), в отличие от
`cp`, который может скопировать файл в момент активной записи и получить
повреждённую копию.

Копия сжимается gzip (coop_ГГГГММДД_ЧЧММСС.db.gz) — SQLite-файл с
показаниями и индексами сжимается ~в 5 раз за секунду; scripts/restore_db.py
распаковывает сам, вручную — `gunzip -k`. Старые несжатые бэкапы (.db)
остаются в ротации наравне со сжатыми.

Хранит последние KEEP_BACKUPS копий (по умолчанию 30 — месяц ежедневных
бэкапов), старые удаляет автоматически.

Запуск вручную:
    cd /path/to/project && python3 scripts/backup_db.py

Обычно запускается через обёртку scripts/backup_db.sh (лог, venv), которую
и добавляют в crontab.
"""
import datetime as dt
import gzip
import os
import shutil
import sqlite3
import sys

# Пути — по тем же правилам, что и config.py (INSTANCE_DIR, DATABASE_URL),
# см. scripts/_paths.py; каталог скрипта в sys.path явно — скрипт
# запускают и как `python3 scripts/…`, и загружают по пути (тесты).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import backup_dir, db_path  # noqa: E402

DB_PATH = db_path()
BACKUP_DIR = backup_dir()
KEEP_BACKUPS = int(os.environ.get("KEEP_BACKUPS", "30"))


def backup() -> str:
    if not os.path.exists(DB_PATH):
        print(f"БД не найдена: {DB_PATH} — нечего копировать.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"coop_{timestamp}.db.gz")
    # Снимок — через backup API во временный .db (сжимать живой файл БД
    # нельзя, см. докстринг), затем сжатие; ".tmp" — чтобы ни ротация, ни
    # restore_db не приняли недописанный файл за бэкап.
    snapshot_path = os.path.join(BACKUP_DIR, f"coop_{timestamp}.db.tmp")

    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(snapshot_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    try:
        with open(snapshot_path, "rb") as raw, gzip.open(backup_path + ".tmp", "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed, 1024 * 1024)
        os.replace(backup_path + ".tmp", backup_path)
    finally:
        os.remove(snapshot_path)

    return backup_path


def is_backup_name(name: str) -> bool:
    return name.startswith("coop_") and name.endswith((".db", ".db.gz"))


def rotate_old_backups() -> int:
    if not os.path.isdir(BACKUP_DIR):
        return 0
    backups = sorted(
        (f for f in os.listdir(BACKUP_DIR) if is_backup_name(f)),  # имя начинается с метки времени — сортировка по нему = по дате
    )
    to_delete = backups[:-KEEP_BACKUPS] if KEEP_BACKUPS > 0 else []
    for name in to_delete:
        os.remove(os.path.join(BACKUP_DIR, name))
    return len(to_delete)


if __name__ == "__main__":
    path = backup()
    size_kb = os.path.getsize(path) / 1024
    print(f"Бэкап создан: {path} ({size_kb:.0f} КБ)")
    removed = rotate_old_backups()
    if removed:
        print(f"Удалено старых бэкапов: {removed} (оставлено последних {KEEP_BACKUPS})")
