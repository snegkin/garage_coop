"""
Окно прогресса долгих операций (app/progress.py): шаги, HTTP-запросы
внутри шага (настоящий локальный HTTP-сервер), поток NDJSON через настоящие
роуты (Telegram, СМС) — с g.user в рабочем потоке (автор записи журнала),
ping при долгом ожидании, внутренняя ошибка в окне; без JS — flash, как было.
"""
import http.server
import json
import threading
from unittest.mock import patch

import pytest
import requests

from app import progress, telegram_bot
from app.bank_api import crypto
from app.models import AuditLog, RoleEnum, SmsSettings, TelegramSettings
from tests.conftest import login, make_person, make_user

NDJSON = {"Accept": "application/x-ndjson"}


def _collect():
    events = []
    return events, progress.Progress(events.append)


def test_null_progress_does_nothing_and_keeps_exceptions():
    with progress.NULL.step("шаг") as step:
        step.ok("готово")
    with pytest.raises(ValueError):
        with progress.NULL.step("шаг"):
            raise ValueError("boom")


def test_step_statuses():
    events, p = _collect()
    with p.step("Первый"):
        pass
    with p.step("Второй") as step:
        step.warn("так себе", "подсказка")
    with pytest.raises(RuntimeError):
        with p.step("Третий"):
            raise RuntimeError("сломалось")
    finals = [(e["title"], e["status"], e["detail"]) for e in events if e["status"] != "running"]
    assert finals == [("Первый", "ok", ""), ("Второй", "warn", "так себе"), ("Третий", "fail", "сломалось")]
    assert [e["status"] for e in events if e["title"] == "Первый"] == ["running", "ok"]


def test_step_failed_carries_hint():
    events, p = _collect()
    with pytest.raises(progress.StepFailed):
        with p.step("Шаг"):
            raise progress.StepFailed("нет доступа", "проверьте ключ")
    assert events[-1]["status"] == "fail" and events[-1]["hint"] == "проверьте ключ"


@pytest.fixture
def http_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = 404 if self.path.startswith("/missing") else 200
            self.send_response(code)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_requests_inside_step_are_shown(app, http_server):
    events, p = _collect()
    with progress.observe(p):
        with p.step("Запросы") as step:
            requests.get(http_server + "/data?token=secret", timeout=5)
            requests.get(http_server + "/missing", timeout=5)
            step_id = step.id
    subs = [e for e in events if e.get("sub") and e["status"] != "running"]
    assert [s["parent"] for s in subs] == [step_id, step_id]
    assert subs[0]["title"] == f"GET {http_server[len('http://'):]}/data"   # без параметров с токеном
    assert subs[0]["status"] == "ok" and subs[0]["detail"].startswith("200, ")
    assert subs[1]["status"] == "warn" and subs[1]["detail"].startswith("404, ")


def test_http_connection_error_is_shown(app):
    events, p = _collect()
    with progress.observe(p), pytest.raises(requests.ConnectionError):
        with p.step("Запрос"):
            requests.get("http://127.0.0.1:9/", timeout=2)   # порт discard — закрыт
    sub = [e for e in events if e.get("sub") and e["status"] != "running"][0]
    assert sub["status"] == "fail" and "отказал" in sub["detail"]


def test_http_outside_window_is_not_traced(app, http_server):
    events, p = _collect()
    requests.get(http_server + "/data", timeout=5)  # без observe()
    assert events == []


# --- роуты ------------------------------------------------------------------------

def _chairman(db, client):
    person = make_person(db, full_name="Chairman One")
    make_user(db, "chair1", "pass1234", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()
    login(client, "chair1", "pass1234")


def _lines(resp):
    return [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]


def test_route_streams_steps_and_result(db, client, monkeypatch):
    _chairman(db, client)
    db.add(TelegramSettings(bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("123:ABC")))
    db.commit()
    monkeypatch.setattr(telegram_bot, "get_me", lambda settings: {"username": "coop_bot"})

    resp = client.post("/telegram/test-connection", headers=NDJSON)
    assert resp.mimetype == "application/x-ndjson"
    assert resp.headers["X-Accel-Buffering"] == "no"
    lines = _lines(resp)
    assert [(e["type"], e.get("status")) for e in lines] == [("step", "running"), ("step", "ok"), ("done", None)]
    assert lines[1]["detail"] == "@coop_bot"
    assert lines[-1] == {"type": "done", "ok": True, "message": "Подключение работает: бот @coop_bot.",
                         "redirect": "/telegram/", "category": "success"}


def test_route_without_js_flashes_as_before(db, client, monkeypatch):
    _chairman(db, client)
    db.add(TelegramSettings(bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("123:ABC")))
    db.commit()

    def fail(settings):
        raise telegram_bot.TelegramError("Unauthorized")

    monkeypatch.setattr(telegram_bot, "get_me", fail)
    resp = client.post("/telegram/test-connection", follow_redirects=True)
    assert "Не удалось подключиться: Unauthorized" in resp.get_data(as_text=True)
    db.expire_all()
    assert db.query(TelegramSettings).one().last_error == "Unauthorized"


def test_worker_keeps_user_for_audit(db, client):
    """Задача — в рабочем потоке: автор записи журнала — тот же пользователь."""
    from app.sms import SmsError

    _chairman(db, client)
    with patch("app.sms_settings.get_sms_client") as factory:
        factory.return_value.send.side_effect = SmsError("недостаточно средств")
        lines = _lines(client.post("/sms/test", data={"test_phone": "9001234567"}, headers=NDJSON))

    assert lines[-1]["ok"] is False and lines[-1]["message"] == "недостаточно средств"
    db.expire_all()
    assert db.query(SmsSettings).one().last_test_result == "недостаточно средств"
    entry = db.query(AuditLog).filter_by(action="sms.test_failed").one()
    assert entry.actor_username == "chair1"


def test_ping_while_waiting_and_internal_error(app, db, client, monkeypatch):
    _chairman(db, client)
    db.add(TelegramSettings(bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("123:ABC")))
    db.commit()
    monkeypatch.setattr(progress, "PING_INTERVAL", 0.05)

    def slow_then_broken(settings):
        threading.Event().wait(0.3)
        raise KeyError("username")  # не TelegramError — непредвиденное

    monkeypatch.setattr(telegram_bot, "get_me", slow_then_broken)
    lines = _lines(client.post("/telegram/test-connection", headers=NDJSON))
    assert any(e["type"] == "ping" for e in lines)
    assert lines[-1]["type"] == "done" and lines[-1]["ok"] is False
    assert lines[-1]["message"].startswith("Внутренняя ошибка")


def test_bank_statement_in_window_saves_lines(app, db, client, monkeypatch):
    """Главный сценарий: выписка банка из окна — шаги «выписка» и
    «сохранение» с итогами, строки записаны рабочим потоком."""
    import datetime as dt
    from decimal import Decimal

    from app import bank_sync
    from app.bank_api.base import StatementLine
    from app.models import BankAccount, BankApiProvider, BankStatementLine
    from tests.test_bank_sync import make_bank_account, make_credential

    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account)
    _chairman(db, client)

    class _Client:
        rotated_refresh_token = None

        def get_statement(self, date_from, date_to):
            return [StatementLine("op-1", dt.date(2026, 8, 1), "debit", Decimal("100.00"), "ООО Свет")]

    monkeypatch.setattr(bank_sync, "get_client", lambda acc: _Client())
    resp = client.post(f"/cooperative/bank-accounts/{account.id}/sync-statement",
                       data={"date_from": "2026-08-01", "date_to": "2026-08-02"}, headers=NDJSON)
    lines = _lines(resp)
    finals = [(e["title"], e["detail"]) for e in lines if e["type"] == "step" and e["status"] == "ok"]
    assert finals == [("Выписка из банка за 01.08.2026 — 02.08.2026", "операций в выписке: 1"),
                      ("Сохранение и разнесение по лицевым счетам", "новых операций: 1, разнесено автоматически: 0")]
    assert lines[-1]["ok"] is True
    assert lines[-1]["redirect"].endswith("date_from=2026-08-01&date_to=2026-08-02")
    database_rows = db.query(BankStatementLine).filter_by(bank_account_id=account.id).count()
    assert database_rows == 1
