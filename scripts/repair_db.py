#!/usr/bin/env python3
"""
Проверка и самолечение БД при старте веб-процесса — для запуска перед ним
(см. scripts/repair_db.sh и README.md, раздел «Автоматизация», systemd
ExecStartPre=).

Мотивация: SQLite в режиме WAL (см. app/database.py) переживает падение
самого процесса без повреждения данных, но если процесс убит ЖЁСТКО
посреди записи (например, OOM killer — тогда systemd, раз это демон,
перезапускает его автоматически, но саму БД никто не проверяет) —
изредка ломаются именно B-tree ИНДЕКСОВ, а не сами данные (наблюдалось
на живой БД: `PRAGMA integrity_check` падал с "database disk image is
malformed" на конкретной таблице, но полный скан её данных без индекса
проходил без единой ошибки — типичный признак повреждения только
индекса). В такой ситуации `REINDEX` чинит БД полностью, не теряя ни
строки — он читает исключительно табличные данные (уже подтверждённо
целые) и просто перестраивает по ним структуры индексов заново.

Что делает:
  1. PRAGMA integrity_check — если "ok", ничего не делает, выход 0.
  2. Иначе REINDEX (пересобирает ВСЕ индексы БД разом — дешевле и проще,
     чем вычислять, какая именно таблица/индекс повреждены) и проверяет
     integrity_check ещё раз.
  3. Если стало "ok" — самолечение сработало, выход 0.
  4. Если всё ещё не "ok" — REINDEX не помог, значит повреждены сами
     СТРАНИЦЫ С ДАННЫМИ, не только индексы: это НЕЛЬЗЯ чинить
     автоматически без риска молча потерять данные, поэтому скрипт
     сознательно НЕ пытается сам восстановить БД из бэкапа — выходит с
     кодом 1 и понятным сообщением, что нужно руками
     `python3 scripts/restore_db.py --restore latest` (это и есть смысл
     ExecStartPre=: ненулевой код не даёт systemd запустить сам сервис,
     чем тихо отдавать 500-е на битой БД).

Использование:
    python3 scripts/repair_db.py

Обычно запускается через обёртку scripts/repair_db.sh (venv, лог), которую
и добавляют в systemd-юнит веб-процесса как ExecStartPre=.
"""
import os
import sqlite3
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(PROJECT_DIR, "instance", "coop.db"))


def _integrity_check(con: sqlite3.Connection) -> tuple[bool, list[str]]:
    rows = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
    return (len(rows) == 1 and rows[0] == "ok"), rows


def check_and_repair(db_path: str) -> int:
    """Возвращает код выхода: 0 — БД здорова (сразу или после REINDEX),
    1 — повреждение осталось после REINDEX, нужно ручное восстановление."""
    if not os.path.exists(db_path):
        print(f"БД не найдена: {db_path} — нечего проверять (первый запуск?).")
        return 0

    con = sqlite3.connect(db_path)
    try:
        ok, rows = _integrity_check(con)
        if ok:
            print(f"{db_path}: integrity_check ok, ремонт не нужен.")
            return 0

        print(f"{db_path}: integrity_check обнаружил проблемы:")
        for row in rows[:20]:
            print(f"  {row}")
        print("Пробую REINDEX (безопасно — читает только данные таблиц, "
              "перестраивает только структуры индексов)...")

        con.execute("REINDEX")
        con.commit()

        ok, rows = _integrity_check(con)
        if ok:
            print(f"{db_path}: REINDEX помог, integrity_check снова ok.")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return 0

        print(
            f"{db_path}: REINDEX не устранил проблему — похоже, повреждены "
            f"сами данные, не только индексы. Автоматическое восстановление "
            f"из бэкапа НЕ выполняется (может тихо откатить свежие данные) — "
            f"нужно решение человека:\n"
            f"  python3 {os.path.join(PROJECT_DIR, 'scripts', 'restore_db.py')} --list\n"
            f"  python3 {os.path.join(PROJECT_DIR, 'scripts', 'restore_db.py')} --restore latest",
            file=sys.stderr,
        )
        for row in rows[:20]:
            print(f"  {row}", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(check_and_repair(DB_PATH))
