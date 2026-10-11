"""scripts/backup_db.py (сжатые бэкапы .db.gz) и scripts/restore_db.py."""
import gzip
import importlib.util
import os
import sqlite3

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _load(name, monkeypatch, db_path, backup_dir):
    # Пути скрипты читают при импорте (DB_PATH/BACKUP_DIR) — окружение до загрузки.
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", os.path.join(SCRIPTS_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def paths(tmp_path):
    db_path = tmp_path / "coop.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('исходные данные')")
    con.commit()
    con.close()
    return db_path, tmp_path / "backups"


def _value(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT v FROM t").fetchone()[0]
    finally:
        con.close()


def test_backup_is_gzipped_sqlite_snapshot(monkeypatch, paths):
    db_path, backup_dir = paths
    backup_db = _load("backup_db", monkeypatch, db_path, backup_dir)

    path = backup_db.backup()
    assert path.endswith(".db.gz")
    assert os.listdir(backup_dir) == [os.path.basename(path)]  # временных файлов не осталось
    unpacked = backup_dir / "check.db"
    unpacked.write_bytes(gzip.decompress(open(path, "rb").read()))
    assert _value(unpacked) == "исходные данные"


def test_rotation_counts_old_plain_and_new_gzipped_backups(monkeypatch, paths):
    db_path, backup_dir = paths
    monkeypatch.setenv("KEEP_BACKUPS", "2")
    backup_db = _load("backup_db", monkeypatch, db_path, backup_dir)
    backup_dir.mkdir()
    for name in ("coop_20261001_030000.db", "coop_20261002_030000.db", "coop_20261003_030000.db.gz", "other.txt"):
        (backup_dir / name).write_bytes(b"x")

    assert backup_db.rotate_old_backups() == 1
    assert sorted(os.listdir(backup_dir)) == ["coop_20261002_030000.db", "coop_20261003_030000.db.gz", "other.txt"]


def test_restore_from_gzipped_backup(monkeypatch, paths):
    db_path, backup_dir = paths
    backup_name = os.path.basename(_load("backup_db", monkeypatch, db_path, backup_dir).backup())
    con = sqlite3.connect(db_path)
    con.execute("UPDATE t SET v = 'испорчено'")
    con.commit()
    con.close()

    restore_db = _load("restore_db", monkeypatch, db_path, backup_dir)
    backups = restore_db.list_backups()
    assert backups == [backup_name]
    assert restore_db.resolve_backup(backup_name.removesuffix(".db.gz"), backups) == backup_name
    restore_db.restore(backup_name, skip_confirm=True)

    assert _value(db_path) == "исходные данные"
    assert not [f for f in os.listdir(backup_dir) if f.endswith(".unpacked")]
    assert any(f.startswith("pre_restore_") for f in os.listdir(backup_dir))
