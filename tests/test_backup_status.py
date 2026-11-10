"""app/backup_status.py — предупреждения о несвежих бэкапах на панели
председателя."""
import datetime as dt
import json
import os

from app.backup_status import backup_warnings
from app.models import RoleEnum

from tests.conftest import make_user, login

NOW = dt.datetime(2026, 11, 10, 12, 0)


def _status(log_dir, finished, ok=True, restic=True, error=None):
    log_dir.mkdir(exist_ok=True)
    (log_dir / "backup_offsite_status.json").write_text(json.dumps({
        "finished_at": finished.isoformat(timespec="seconds"), "ok": ok, "restic": restic, "error": error,
    }))


def _pull(log_dir, moment):
    log_dir.mkdir(exist_ok=True)
    with open(log_dir / "backup_pull.log", "a") as fh:
        fh.write(f"{moment.astimezone().isoformat(timespec='seconds')} 1.2.3.4 rsync --server --sender .\n")


def test_all_fresh_no_warnings(app, tmp_path):
    logs = tmp_path / "logs"
    _status(logs, NOW - dt.timedelta(hours=9))
    _pull(logs, NOW - dt.timedelta(days=5))
    _pull(logs, NOW - dt.timedelta(hours=1))
    with app.test_request_context():
        assert backup_warnings(str(logs), str(tmp_path / "backups"), now=NOW) == []


def test_never_run_warns_only_where_local_backups_are_fresh(app, tmp_path):
    logs, backups = tmp_path / "logs", tmp_path / "backups"
    with app.test_request_context():
        assert backup_warnings(str(logs), str(backups), now=NOW) == []  # машина разработчика
        backups.mkdir()
        snapshot = backups / "coop_20261110_030000.db.gz"
        snapshot.write_bytes(b"x")
        os.utime(snapshot, (NOW.timestamp(), NOW.timestamp()))
        [warning] = backup_warnings(str(logs), str(backups), now=NOW)
        assert "ни разу не запускался" in warning


def test_error_stale_unconfigured_and_agent(app, tmp_path):
    logs = tmp_path / "logs"
    with app.test_request_context():
        _status(logs, NOW, ok=False, error="restic backup (код 1): network")
        warnings = backup_warnings(str(logs), "", now=NOW)
        assert "ошибкой: restic backup (код 1): network" in warnings[0]
        assert "ни разу не забирал" in warnings[1]

        _status(logs, NOW - dt.timedelta(days=2))
        _pull(logs, NOW - dt.timedelta(days=4))
        warnings = backup_warnings(str(logs), "", now=NOW)
        assert "не запускался с 08.11.2026 12:00" in warnings[0]
        assert "не обновлялась с 06.11.2026 12:00" in warnings[1]

        _status(logs, NOW, restic=False)
        _pull(logs, NOW)
        [warning] = backup_warnings(str(logs), "", now=NOW)
        assert "не настроен" in warning


def test_dashboard_shows_warning_to_chairman_only(app, db, client, tmp_path):
    log_dir = tmp_path / "logs"
    app.config["LOG_FOLDER"] = str(log_dir)
    _status(log_dir, dt.datetime.now(), ok=False, error="диск полон")
    make_user(db, "chair_b", "pass12345", role=RoleEnum.CHAIRMAN)
    make_user(db, "board_b", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_b", "pass12345")
    assert "диск полон" not in client.get("/dashboard").get_data(as_text=True)
    client.get("/logout")
    login(client, "chair_b", "pass12345")
    assert "Внешний бэкап завершился ошибкой: диск полон" in client.get("/dashboard").get_data(as_text=True)
