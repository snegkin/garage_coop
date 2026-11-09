"""Согласованный снимок SQLite-БД для переноса на сервер (запускается локально).

    python3 db_snapshot.py ИСХОДНАЯ.db СНИМОК.db

Копирует через sqlite3 backup API: годится, даже если локальный сайт сейчас
запущен и пишет в БД (голый cp мог бы захватить файл посреди записи и
потерять то, что ещё лежит в -wal). Проверяет PRAGMA integrity_check и
печатает JSON: {"integrity": "ok", "revision": "<ревизия alembic>", "size": N}.
"""
import json
import os
import sqlite3
import sys


def main(src: str, dst: str) -> int:
    if os.path.exists(dst):
        os.remove(dst)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    with target:
        source.backup(target)
    source.close()
    # Снимок — самостоятельный файл, без -wal рядом.
    target.execute("PRAGMA journal_mode=DELETE")
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    try:
        revision = target.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    except sqlite3.Error:
        revision = None
    target.close()
    print(json.dumps({"integrity": integrity, "revision": revision, "size": os.path.getsize(dst)}))
    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
