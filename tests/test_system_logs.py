"""Просмотр логов сервера (app/system_logs.py)."""
import pytest

from app import system_logs
from app.models import RoleEnum
from tests.conftest import make_user, login


@pytest.fixture
def log_dir(app, tmp_path):
    app.config["LOG_FOLDER"] = str(tmp_path)
    (tmp_path / "sync_bank_accounts.log").write_text(
        "строка 1\n[2026-10-02] выписка — ОШИБКА: сбой\nстрока 3\n", encoding="utf-8",
    )
    (tmp_path / "secret.txt").write_text("не лог")
    return tmp_path


def _chairman(db, client):
    make_user(db, "chair_logs", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair_logs", "pass12345")


def test_index_lists_only_log_files(app, db, client, log_dir):
    _chairman(db, client)
    html = client.get("/system-logs/").get_data(as_text=True)
    assert "sync_bank_accounts.log" in html
    assert "secret.txt" not in html


def test_view_highlights_errors_and_filters(app, db, client, log_dir):
    _chairman(db, client)
    html = client.get("/system-logs/sync_bank_accounts.log").get_data(as_text=True)
    assert "строка 1" in html and "text-danger" in html

    html = client.get("/system-logs/sync_bank_accounts.log?errors=1").get_data(as_text=True)
    assert "ОШИБКА: сбой" in html and "строка 1" not in html

    html = client.get("/system-logs/sync_bank_accounts.log?q=СТРОКА 3").get_data(as_text=True)
    assert "строка 3" in html and "строка 1" not in html


def test_download(app, db, client, log_dir):
    _chairman(db, client)
    resp = client.get("/system-logs/sync_bank_accounts.log/download")
    assert resp.status_code == 200 and "ОШИБКА" in resp.get_data(as_text=True)


@pytest.mark.parametrize("name", ["secret.txt", "..%2Fcoop.db", "missing.log"])
def test_files_outside_list_are_not_served(app, db, client, log_dir, name):
    _chairman(db, client)
    assert client.get(f"/system-logs/{name}").status_code == 404


def test_board_member_has_no_access(app, db, client, log_dir):
    make_user(db, "board_logs", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_logs", "pass12345")
    resp = client.get("/system-logs/sync_bank_accounts.log")
    assert resp.status_code in (302, 403)
    assert "ОШИБКА" not in resp.get_data(as_text=True)


def test_read_tail_returns_last_lines_of_big_file(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(100_000)))
    lines, truncated = system_logs.read_tail(str(path), 200)
    assert truncated and len(lines) == 200
    assert lines[0] == "line 99800" and lines[-1] == "line 99999"


def test_read_tail_small_file_not_truncated(tmp_path):
    path = tmp_path / "small.log"
    path.write_text("a\nb\n")
    assert system_logs.read_tail(str(path), 200) == (["a", "b"], False)
