"""
scripts/repair_db.py — автоматическое самолечение БД перед стартом
веб-процесса (см. README.md, раздел «Автоматизация», systemd ExecStartPre=).

Настоящую порчу файла SQLite байтами в тесте не воспроизвести надёжно
(зависит от версии libsqlite3 и точной раскладки страниц) — вместо этого
подставляем поддельное соединение (класс _FakeConnection ниже), которое
воспроизводит ИМЕННО ту последовательность ответов PRAGMA integrity_check,
которая наблюдалась на реальной повреждённой БД (см. коммит, где скрипт
появился): до REINDEX — список проблем, после — "ok". Так тестируется
именно логика скрипта (когда чинить, когда сдаваться), не сам SQLite.
"""
import importlib.util
import os
import sqlite3

import pytest


def _load_repair_db_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "repair_db.py")
    spec = importlib.util.spec_from_file_location("repair_db_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def repair_db_module():
    return _load_repair_db_module()


class _FakeConnection:
    """Подменяет sqlite3.connect() — не открывает реальный файл, просто
    отдаёт заранее заданную последовательность результатов
    integrity_check и запоминает, какие команды выполнялись."""

    def __init__(self, integrity_results):
        self._integrity_results = list(integrity_results)
        self.executed = []

    def execute(self, sql, *args):
        self.executed.append(sql)
        if sql.strip().upper().startswith("PRAGMA INTEGRITY_CHECK"):
            result = self._integrity_results.pop(0)
            return _FakeCursor(result)
        return _FakeCursor([])

    def commit(self):
        pass

    def close(self):
        pass


class _FakeCursor:
    def __init__(self, rows):
        self._rows = [(r,) for r in rows]

    def fetchall(self):
        return self._rows


def test_missing_db_file_is_not_an_error(repair_db_module, tmp_path):
    missing_path = str(tmp_path / "does_not_exist.db")
    assert repair_db_module.check_and_repair(missing_path) == 0


def test_healthy_db_needs_no_repair(repair_db_module, tmp_path, monkeypatch):
    db_path = str(tmp_path / "healthy.db")
    sqlite3.connect(db_path).close()  # реальный пустой файл — только os.path.exists важен

    fake_con = _FakeConnection(integrity_results=[["ok"]])
    monkeypatch.setattr(sqlite3, "connect", lambda path: fake_con)

    assert repair_db_module.check_and_repair(db_path) == 0
    assert not any("REINDEX" in cmd.upper() for cmd in fake_con.executed)


def test_index_corruption_is_fixed_by_reindex(repair_db_module, tmp_path, monkeypatch):
    db_path = str(tmp_path / "corrupted.db")
    sqlite3.connect(db_path).close()

    fake_con = _FakeConnection(integrity_results=[
        ["database disk image is malformed"],  # первая проверка — повреждено
        ["ok"],  # после REINDEX — здорово
    ])
    monkeypatch.setattr(sqlite3, "connect", lambda path: fake_con)

    assert repair_db_module.check_and_repair(db_path) == 0
    assert any(cmd.strip().upper() == "REINDEX" for cmd in fake_con.executed)


def test_data_corruption_survives_reindex_and_reports_failure(repair_db_module, tmp_path, monkeypatch):
    """REINDEX не панацея — если повреждены сами данные, а не индекс,
    integrity_check останется плохим и после REINDEX. Скрипт должен
    сдаться (код 1), а не притвориться, что всё исправил."""
    db_path = str(tmp_path / "corrupted.db")
    sqlite3.connect(db_path).close()

    fake_con = _FakeConnection(integrity_results=[
        ["database disk image is malformed"],
        ["database disk image is malformed"],
    ])
    monkeypatch.setattr(sqlite3, "connect", lambda path: fake_con)

    assert repair_db_module.check_and_repair(db_path) == 1
    assert any(cmd.strip().upper() == "REINDEX" for cmd in fake_con.executed)
