"""
Агент локального бэкапа (backup_agent/coop_backup_agent.py) — отдельная
программа для компьютера председателя; здесь rsync/restic подменены
(subprocess.run): rsync копирует из каталога-«сервера», restic только
записывает вызовы.
"""
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backup_agent", "coop_backup_agent.py")
_spec = importlib.util.spec_from_file_location("coop_backup_agent", _PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)


@pytest.fixture()
def server(tmp_path):
    inst = tmp_path / "server"
    (inst / "backup_staging").mkdir(parents=True)
    (inst / "backup_staging" / "coop.db").write_bytes(b"db")
    (inst / "uploads").mkdir()
    (inst / "uploads" / "doc.pdf").write_bytes(b"%PDF")
    _manifest(inst, dt.datetime.now(), ["uploads"])
    return inst


def _manifest(inst, created, dirs):
    (inst / "backup_staging" / "MANIFEST.json").write_text(json.dumps(
        {"created_at": created.isoformat(timespec="seconds"), "data_dirs": dirs}))


@pytest.fixture()
def cfg(tmp_path):
    path = tmp_path / "coop_backup.ini"
    path.write_text(f"""
[server]
host = coop.example.ru
user = coop
identity_file = /etc/coop-backup/id_ed25519
known_hosts = /etc/coop-backup/known_hosts
[local]
mirror_dir = {tmp_path}/mirror
repository = {tmp_path}/repo
password_file = {tmp_path}/pw
state_dir = {tmp_path}/state
""", encoding="utf-8")
    return agent.Config(str(path))


@pytest.fixture()
def fake_run(monkeypatch, server):
    calls = []
    state = {"cat_rc": 0, "rsync_rc": 0}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "rsync":
            if state["rsync_rc"]:
                return subprocess.CompletedProcess(argv, state["rsync_rc"], "", "connection refused")
            remote = argv[-2].split(":", 1)[1].rstrip("/")
            shutil.copytree(server / remote, argv[-1], dirs_exist_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["cat", "config"]:
            return subprocess.CompletedProcess(argv, state["cat_rc"], "", "")
        if argv[1] == "backup":
            return subprocess.CompletedProcess(argv, 0, '{"message_type":"summary","snapshot_id":"s1"}\n', "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(agent.subprocess, "run", run)
    return calls, state


def _status(cfg):
    return json.loads(open(os.path.join(cfg.state_dir, "status.json"), encoding="utf-8").read())


def test_run_pulls_manifest_dirs_and_backs_up(cfg, fake_run):
    calls, _ = fake_run
    assert agent.run(cfg) == 0

    rsyncs = [argv for argv, _ in calls if argv[0] == "rsync"]
    assert [argv[-2] for argv in rsyncs] == ["coop@coop.example.ru:backup_staging/", "coop@coop.example.ru:uploads/"]
    assert "--delete" in rsyncs[0]
    ssh = rsyncs[0][rsyncs[0].index("-e") + 1]
    assert "BatchMode=yes" in ssh and "StrictHostKeyChecking=yes" in ssh
    assert open(os.path.join(cfg.mirror_dir, "uploads", "doc.pdf"), "rb").read() == b"%PDF"

    backup = next((argv, kw) for argv, kw in calls if argv[1] == "backup")
    assert backup[0][-2:] == ["backup_staging", "uploads"]
    assert backup[1]["cwd"] == cfg.mirror_dir
    assert backup[1]["env"]["RESTIC_REPOSITORY"] == cfg.repository
    assert any(argv[1] == "forget" for argv, _ in calls)
    status = _status(cfg)
    assert status["ok"] is True and status["snapshot_id"] == "s1" and status["warning"] is None


def test_stale_server_snapshot_still_backed_up_with_warning(cfg, fake_run, server):
    _manifest(server, dt.datetime.now() - dt.timedelta(days=3), ["uploads"])
    assert agent.run(cfg) == 2
    status = _status(cfg)
    assert status["ok"] is True and "не работает cron" in status["warning"]


def test_missing_repository_initialised(cfg, fake_run):
    calls, state = fake_run
    state["cat_rc"] = 10
    assert agent.run(cfg) == 0
    assert [argv[1] for argv, _ in calls if argv[0] != "rsync"][:3] == ["cat", "init", "backup"]


def test_rsync_failure_is_error_without_backup(cfg, fake_run):
    calls, state = fake_run
    state["rsync_rc"] = 255
    assert agent.run(cfg) == 1
    assert not any(argv[0] != "rsync" for argv, _ in calls)
    assert "connection refused" in _status(cfg)["error"]


def test_manifest_dir_names_are_not_paths(cfg, fake_run, server):
    _manifest(server, dt.datetime.now(), ["../etc"])
    assert agent.run(cfg) == 1
    assert "недопустимое имя" in _status(cfg)["error"]
