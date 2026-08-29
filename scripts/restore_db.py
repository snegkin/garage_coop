#!/usr/bin/env python3
"""
Восстановление БД из бэкапа, созданного scripts/backup_db.py.

Без аргументов — показывает список доступных бэкапов в BACKUP_DIR (те же
файлы coop_YYYYMMDD_HHMMSS.db) и ничего не меняет. Восстановление —
явное и обязательно подтверждается, т.к. перезаписывает текущую БД.

Использование:
    # посмотреть список бэкапов
    python3 scripts/restore_db.py --list

    # восстановить конкретный (по номеру из --list или по имени файла)
    python3 scripts/restore_db.py --restore 3
    python3 scripts/restore_db.py --restore coop_20260829_030000.db

    # восстановить самый свежий бэкап
    python3 scripts/restore_db.py --restore latest

    # пропустить интерактивное подтверждение (для скриптов/автоматизации)
    python3 scripts/restore_db.py --restore latest --yes

Перед перезаписью текущий файл БД (если есть) сам сохраняется в
BACKUP_DIR с пометкой pre_restore_ — так что откатить восстановление
тоже можно (см. --restore с именем этого файла). Восстановление идёт
через sqlite3 backup API (как и сам бэкап), не через `cp`, и после
записи проверяется `PRAGMA integrity_check`, прежде чем подменить
рабочий файл БД.
"""
import argparse
import datetime as dt
import os
import shutil
import sqlite3
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(PROJECT_DIR, "instance", "coop.db"))
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(PROJECT_DIR, "instance", "backups"))


def list_backups() -> list[str]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith("coop_") and f.endswith(".db")
    )


def print_backups(backups: list[str]) -> None:
    if not backups:
        print(f"В {BACKUP_DIR} нет бэкапов (файлов coop_*.db).")
        return
    print(f"Бэкапы в {BACKUP_DIR}:")
    for i, name in enumerate(backups, start=1):
        path = os.path.join(BACKUP_DIR, name)
        size_kb = os.path.getsize(path) / 1024
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  [{i}] {name}   {size_kb:>8.0f} КБ   {mtime}")


def resolve_backup(selector: str, backups: list[str]) -> str:
    """Возвращает имя файла бэкапа по номеру из списка, имени файла или 'latest'."""
    if not backups:
        print(f"В {BACKUP_DIR} нет бэкапов — восстанавливать нечего.", file=sys.stderr)
        sys.exit(1)

    if selector == "latest":
        return backups[-1]

    if selector.isdigit():
        idx = int(selector)
        if not (1 <= idx <= len(backups)):
            print(
                f"Неверный номер бэкапа: {idx}. Доступны 1..{len(backups)} "
                f"(см. --list).",
                file=sys.stderr,
            )
            sys.exit(1)
        return backups[idx - 1]

    name = selector if selector.endswith(".db") else f"{selector}.db"
    if name not in backups:
        print(f"Бэкап не найден: {name} (в {BACKUP_DIR}). См. --list.", file=sys.stderr)
        sys.exit(1)
    return name


def verify_integrity(path: str) -> bool:
    con = sqlite3.connect(path)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        return bool(result) and result[0] == "ok"
    finally:
        con.close()


def restore(backup_name: str, skip_confirm: bool) -> None:
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.exists(backup_path):
        print(f"Файл бэкапа не найден: {backup_path}", file=sys.stderr)
        sys.exit(1)

    if not verify_integrity(backup_path):
        print(
            f"Бэкап {backup_name} не прошёл PRAGMA integrity_check — "
            f"файл повреждён, восстановление отменено.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Текущая БД:  {DB_PATH}")
    print(f"Восстановить из: {backup_path}")
    if not skip_confirm:
        answer = input(
            "Это ПЕРЕЗАПИШЕТ текущую БД данными из бэкапа. Продолжить? [yes/no]: "
        ).strip().lower()
        if answer not in ("yes", "y", "да"):
            print("Отменено.")
            sys.exit(0)

    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Сохраняем текущую БД перед перезаписью — на случай, если восстановили
    # не то, что нужно, можно откатиться этим же скриптом.
    if os.path.exists(DB_PATH):
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safety_path = os.path.join(BACKUP_DIR, f"pre_restore_{timestamp}.db")
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(safety_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        print(f"Текущая БД сохранена перед перезаписью: {safety_path}")

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    # Восстанавливаем через backup API во временный файл рядом с целевым —
    # атомарный os.replace() в конце гарантирует, что при сбое посреди
    # записи текущая рабочая БД останется нетронутой (в отличие от прямой
    # перезаписи DB_PATH построчно).
    tmp_path = DB_PATH + ".restoring"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    source = sqlite3.connect(backup_path)
    dest = sqlite3.connect(tmp_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    if not verify_integrity(tmp_path):
        os.remove(tmp_path)
        print("Восстановленный файл не прошёл integrity_check — отменено.", file=sys.stderr)
        sys.exit(1)

    os.replace(tmp_path, DB_PATH)

    # SQLite в режиме WAL может оставить рядом с БД файлы -wal/-shm от
    # предыдущего состояния — они относятся к старым данным и должны быть
    # убраны, иначе при следующем открытии БД может подхватиться их
    # содержимое поверх восстановленных данных.
    for suffix in ("-wal", "-shm"):
        stale = DB_PATH + suffix
        if os.path.exists(stale):
            os.remove(stale)

    print(f"Готово: {DB_PATH} восстановлена из {backup_name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Восстановление БД из бэкапа.")
    parser.add_argument(
        "--list", action="store_true", help="показать список доступных бэкапов и выйти"
    )
    parser.add_argument(
        "--restore",
        metavar="N|ИМЯ|latest",
        help="номер из --list, имя файла бэкапа или 'latest' (самый свежий)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="не спрашивать подтверждение (для автоматизации)"
    )
    args = parser.parse_args()

    backups = list_backups()

    if args.restore is None:
        print_backups(backups)
        if not args.list:
            print("\nЧтобы восстановить, укажите --restore N|ИМЯ|latest (см. --help).")
        return

    backup_name = resolve_backup(args.restore, backups)
    restore(backup_name, skip_confirm=args.yes)


if __name__ == "__main__":
    main()
