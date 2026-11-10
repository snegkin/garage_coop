"""scripts/backup_offsite.py — каталог для забора и вызовы restic (restic
подменён скриптом-заглушкой, который пишет свои аргументы в журнал)."""
import importlib.util
import json
import os
import sqlite3
import stat
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

FAKE_RESTIC = """#!/bin/sh
echo "$@" >> "$FAKE_RESTIC_LOG"
case "$1" in
    cat) exit "${FAKE_RESTIC_CAT_RC:-0}" ;;
    backup) echo '{"message_type":"summary","snapshot_id":"abc123","total_files_processed":3,"data_added":1048576}' ;;
esac
exit 0
"""


@pytest.fixture
def instance(tmp_path, monkeypatch):
    inst = tmp_path / "instance"
    inst.mkdir()
    con = sqlite3.connect(inst / "coop.db")
    con.execute("CREATE TABLE alembic_version (version_num TEXT)")
    con.execute("INSERT INTO alembic_version VALUES ('rev42')")
    con.commit()
    con.close()
    (inst / "uploads").mkdir()
    (inst / "uploads" / "doc.pdf").write_bytes(b"%PDF")
    (inst / "bank_certs").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=s\n")

    restic = tmp_path / "restic"
    restic.write_text(FAKE_RESTIC)
    restic.chmod(restic.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("INSTANCE_DIR", str(inst))
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKUP_STAGING_DIR", raising=False)
    monkeypatch.delenv("RESTIC_REPOSITORY", raising=False)
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("RESTIC_BIN", str(restic))
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(tmp_path / "restic.log"))
    return inst


def _load():
    # _paths читает INSTANCE_DIR при импорте — каждый раз заново.
    sys.modules.pop("_paths", None)
    spec = importlib.util.spec_from_file_location("backup_offsite_under_test", os.path.join(SCRIPTS_DIR, "backup_offsite.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _restic_calls(instance):
    path = instance.parent / "restic.log"
    return path.read_text().splitlines() if path.exists() else []


def test_without_repository_only_stages(instance):
    module = _load()
    assert module.main() == 0

    staging = instance / "backup_staging"
    con = sqlite3.connect(staging / "coop.db")
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == ("rev42",)
    con.close()
    assert (staging / "env").read_text() == "SECRET_KEY=s\n"
    assert stat.S_IMODE((staging / "env").stat().st_mode) == 0o600
    manifest = json.loads((staging / "MANIFEST.json").read_text())
    assert manifest["db_revision"] == "rev42"
    assert manifest["env_included"] is True
    assert manifest["data_dirs"] == ["uploads", "bank_certs"]
    assert not [name for name in os.listdir(staging) if name.endswith(".tmp")]

    assert _restic_calls(instance) == []
    status = json.loads((instance / "logs" / "backup_offsite_status.json").read_text())
    assert status["ok"] is True and status["restic"] is False


def test_with_repository_backs_up_relative_paths_and_prunes(instance, monkeypatch):
    monkeypatch.setenv("RESTIC_REPOSITORY", "s3:https://storage.yandexcloud.net/b/coop")
    monkeypatch.setenv("OFFSITE_BACKUP_HOST", "coop-vps")
    module = _load()
    assert module.main() == 0

    calls = _restic_calls(instance)
    assert calls[0] == "cat config"
    assert calls[1] == "backup --json --host coop-vps --tag coop -- backup_staging uploads bank_certs"
    assert calls[2].startswith("forget --host coop-vps --tag coop --prune --keep-daily 14")
    status = json.loads((instance / "logs" / "backup_offsite_status.json").read_text())
    assert status["ok"] is True and status["snapshot_id"] == "abc123"


def test_missing_repository_is_initialised(instance, monkeypatch):
    monkeypatch.setenv("RESTIC_REPOSITORY", "/tmp/repo")
    monkeypatch.setenv("FAKE_RESTIC_CAT_RC", "10")
    monkeypatch.setenv("OFFSITE_PRUNE", "0")
    module = _load()
    assert module.main() == 0
    calls = _restic_calls(instance)
    assert calls[:2] == ["cat config", "init"]
    assert not any(call.startswith("forget") for call in calls)


def test_repository_access_error_fails_without_init(instance, monkeypatch):
    monkeypatch.setenv("RESTIC_REPOSITORY", "/tmp/repo")
    monkeypatch.setenv("FAKE_RESTIC_CAT_RC", "12")  # неверный пароль
    module = _load()
    assert module.main() == 1
    assert _restic_calls(instance) == ["cat config"]
    status = json.loads((instance / "logs" / "backup_offsite_status.json").read_text())
    assert status["ok"] is False and "код 12" in status["error"]
    # Каталог для агента всё равно обновлён.
    assert status["staged"] is True


def test_missing_db_is_error(instance):
    (instance / "coop.db").unlink()
    module = _load()
    assert module.main() == 1
