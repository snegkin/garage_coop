"""Отладочный журнал внешних API (app/api_log.py) — без сети: запросы
уходят в подменённый адаптер сессии requests."""
import datetime as dt
import json

import pytest
import requests
from requests.adapters import BaseAdapter

from app import api_log


class _FakeAdapter(BaseAdapter):
    def __init__(self, status=200, body=b"", content_type="application/json", error=None):
        super().__init__()
        self.status, self.body, self.content_type, self.error = status, body, content_type, error

    def send(self, request, **kwargs):
        if self.error:
            raise self.error
        resp = requests.Response()
        resp.status_code = self.status
        resp._content = self.body
        resp.headers["Content-Type"] = self.content_type
        resp.request = request
        resp.url = request.url
        return resp

    def close(self):
        pass


@pytest.fixture
def log_dir(tmp_path):
    api_log.install(str(tmp_path))
    yield tmp_path
    for h in list(api_log.logger.handlers):
        api_log.logger.removeHandler(h)


def _log_text(log_dir):
    return (log_dir / f"external_api-{dt.date.today().isoformat()}.log").read_text(encoding="utf-8")


def _session(adapter):
    s = requests.Session()
    s.mount("https://", adapter)
    return s


def test_mask_url_hides_secret_params_and_telegram_token():
    url = api_log.mask_url("https://api.telegram.org/bot123:ABC/getUpdates?offset=5&access_token=xyz")
    assert "123:ABC" not in url and "xyz" not in url
    assert "offset=5" in url


def test_format_body_masks_json_secrets_and_truncates():
    body = json.dumps({"access_token": "a1", "refresh_token": "r1", "data": {"amount": 5}}).encode()
    text = api_log.format_body(body, "application/json", 4000)
    assert "a1" not in text and "r1" not in text and '"amount": 5' in text
    assert api_log.format_body("x" * 50, "text/plain", 10).startswith("x" * 10 + "…")


def test_format_body_masks_form_and_skips_binary():
    text = api_log.format_body("grant_type=refresh_token&refresh_token=r1&client_secret=s1", "application/x-www-form-urlencoded", 4000)
    assert "r1" not in text and "s1" not in text and "grant_type=refresh_token" in text
    assert api_log.format_body(b"\x00\x01", "application/pdf", 4000) == "<2 байт, application/pdf>"


def test_request_and_response_are_logged(log_dir):
    body = json.dumps({"transactions": [{"uuid": "op-1"}], "refresh_token": "secret-r"}).encode()
    resp = _session(_FakeAdapter(body=body)).get("https://bank.example/statement", params={"date": "2026-10-02"})
    assert resp.json()["transactions"][0]["uuid"] == "op-1"  # тело ответа не съедено журналом
    text = _log_text(log_dir)
    assert "→ GET https://bank.example/statement?date=2026-10-02" in text
    assert "← 200 GET" in text and "op-1" in text
    assert "secret-r" not in text


def test_connection_error_is_logged_and_reraised(log_dir):
    with pytest.raises(requests.ConnectionError):
        _session(_FakeAdapter(error=requests.ConnectionError("обрыв: url /x?token=t0p"))).get("https://bank.example/x?token=t0p")
    text = _log_text(log_dir)
    assert "✗ GET https://bank.example/x?token=***" in text and "обрыв" in text
    assert "t0p" not in text


def test_old_files_are_removed(tmp_path):
    old = tmp_path / "external_api-2000-01-01.log"
    old.write_text("x")
    api_log.install(str(tmp_path), keep_days=14)
    assert not old.exists()


def test_app_log_writes_warnings_with_traceback(tmp_path):
    import logging
    api_log.install_app_log(str(tmp_path))
    try:
        try:
            raise ValueError("кривая сумма")
        except ValueError as e:
            logging.getLogger("app.errors").warning("Bad form input: %r", e, exc_info=e)
        logging.getLogger("app.x").info("не должно попасть")
        text = (tmp_path / f"app-{dt.date.today().isoformat()}.log").read_text(encoding="utf-8")
        assert "Bad form input" in text and "Traceback" in text and "кривая сумма" in text
        assert "не должно попасть" not in text
    finally:
        app_logger = logging.getLogger("app")
        for h in list(app_logger.handlers):
            if isinstance(h, api_log._DailyFileHandler):
                app_logger.removeHandler(h)
