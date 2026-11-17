"""Быстрый путь run_migrations (app/database.py): head по тексту файлов
миграций должен совпадать с тем, что считает сам Alembic."""
import os

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from app import database


def test_head_revisions_match_alembic():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = ScriptDirectory.from_config(AlembicConfig(os.path.join(root, "alembic.ini")))
    assert database._head_revisions() == set(script.get_heads())


def test_is_at_head_only_for_migrated_db(tmp_path):
    uri = f"sqlite:///{tmp_path / 'coop.db'}"
    assert database._is_at_head(uri) is False  # пустая БД — нужен полный прогон
    database.run_migrations(uri)
    assert database._is_at_head(uri) is True


def _upgrade_to(uri, revision):
    from alembic import command
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = AlembicConfig(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", uri)
    cfg.attributes["db_url_override"] = uri
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, revision)


def test_stale_batch_table_from_interrupted_migration_is_dropped(tmp_path):
    """Сбой на сервере 17.11.2026: прерванная миграция c3f9a2e7d514 оставила
    пустую _alembic_tmp_issued_document, следующая (d8b4e1c6a273) падала на
    «already exists» при каждом старте."""
    import sqlite3
    db_file = tmp_path / "coop.db"
    uri = f"sqlite:///{db_file}"
    _upgrade_to(uri, "c3f9a2e7d514")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE _alembic_tmp_issued_document (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    database.run_migrations(uri)

    assert database._is_at_head(uri)
    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '\\_alembic\\_tmp%' ESCAPE '\\'").fetchall() == []
    columns = {row[1] for row in conn.execute("PRAGMA table_info(issued_document)")}
    assert {"buyer_person_id", "counterparty_id"} <= columns


def _migrate_in_process(uri):
    database.run_migrations(uri)


def test_concurrent_processes_migrate_once(tmp_path):
    """Веб-воркер и cron-скрипт стартуют одновременно на новом коде: оба
    должны дойти до последней ревизии без ошибок (второй ждёт блокировку)."""
    import multiprocessing
    uri = f"sqlite:///{tmp_path / 'coop.db'}"
    _upgrade_to(uri, "c3f9a2e7d514")
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_migrate_in_process, args=(uri,)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0, 0, 0]
    assert database._is_at_head(uri)
