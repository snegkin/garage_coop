"""
Агент ЭЗП (ezp_agent/ezp_agent.py) — отдельная программа для ПК с Рутокеном,
приложение не импортирует; здесь — его логика без настоящих cryptcp/stunnel/
сайта: subprocess.run и requests подменены.
"""
import importlib.util
import json
import os
import subprocess

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ezp_agent", "ezp_agent.py")
_spec = importlib.util.spec_from_file_location("ezp_agent", _PATH)
ezp_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ezp_agent)


@pytest.fixture()
def cfg(tmp_path):
    path = tmp_path / "ezp_agent.ini"
    path.write_text(f"""
[site]
url = https://coop.example.ru/
agent_token = agent-tok
[ezp]
id = 1071
token = ezp-tok
[crypto]
thumbprint = 32c441ef
pin = 1234
[agent]
state_dir = {tmp_path}
lock_file = {tmp_path}/run/agent.lock
""", encoding="utf-8")
    return ezp_agent.Config(str(path))


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def test_config_defaults(cfg):
    assert cfg.site_url == "https://coop.example.ru"
    assert cfg.ezp_url == "http://127.0.0.1:3080"
    assert cfg.sign_extra_args == ["-errchain"]
    assert cfg.poll_interval == 300


def test_cryptcp_args(cfg):
    args = ezp_agent.cryptcp_sign_args(cfg, "/tmp/x/letter.pdf", "/tmp/x")
    assert args[:2] == [cfg.cryptcp, "-signf"]
    assert "-detached" in args and "-der" not in args and "-base64" not in args
    assert args[args.index("-fext") + 1] == ".sig"
    assert args[args.index("-thumbprint") + 1] == "32c441ef"
    assert args[args.index("-pin") + 1] == "1234"
    assert "-errchain" in args and "-cert" in args
    assert args[-1] == "/tmp/x/letter.pdf"


def test_sign_pdf_failure_is_agent_error(cfg, tmp_path, monkeypatch):
    pdf = tmp_path / "letter.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no token"))
    with pytest.raises(ezp_agent.AgentError, match="no token"):
        ezp_agent.sign_pdf(cfg, str(pdf))


def test_letter_multipart_utf8_without_bom():
    parts = ezp_agent.letter_multipart({"title": "Уведомление"}, b"%PDF", b"sig")
    name, data, ctype = parts["commonInfo"]
    assert ctype == "application/json"
    assert not data.startswith(b"\xef\xbb\xbf")
    assert json.loads(data.decode("utf-8")) == {"title": "Уведомление"}
    assert set(parts) == {"commonInfo", "letter.pdf", "letter.pdf.sig"}


@pytest.mark.parametrize("status,exc", [(400, "LetterRejected"), (401, "AgentError"), (403, "AgentError"), (500, "AgentError")])
def test_parse_ezp_response_errors(status, exc):
    with pytest.raises(getattr(ezp_agent, exc)):
        ezp_agent._parse_ezp_response(FakeResponse(status, {"code": status, "message": "x"}))


def test_parse_ezp_response_ok():
    resp = FakeResponse(200, {"code": 200, "data": {"requestCode": "req_1"}})
    assert ezp_agent._parse_ezp_response(resp) == {"requestCode": "req_1"}


def _fake_world(monkeypatch, post_response):
    """Подменяет cryptcp (создаёт .sig) и HTTP: сайт и Почту."""
    calls = []

    def fake_run(args, **kwargs):
        open(args[-1] + ".sig", "wb").write(b"SIG")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_request(method, url, headers=None, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/agent/jobs"):
            return FakeResponse(200, {
                "send": [{"id": 7, "reg_number": "2026-7", "pdf_path": "/postal-letters/agent/jobs/7/pdf",
                          "common_info": {"document": {"id": "gsk-7"}}}],
                "poll": [{"id": 5, "request_code": "req_5"}],
            })
        if url.endswith("/pdf"):
            return FakeResponse(200, content=b"%PDF-1.4")
        return FakeResponse(200, {"ok": True})

    def fake_post(url, headers=None, files=None, **kwargs):
        calls.append(("POST-EZP", url, sorted(files)))
        assert headers["Authorization"] == "Basic MTA3MTplenAtdG9r"  # 1071:ezp-tok
        return post_response

    def fake_get(url, headers=None, **kwargs):
        calls.append(("GET-EZP", url, None))
        return FakeResponse(200, {"code": 200, "data": {"type": "ONLINE_DELIVERED", "mailId": "123"}})

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ezp_agent.requests, "request", fake_request)
    monkeypatch.setattr(ezp_agent.requests, "post", fake_post)
    monkeypatch.setattr(ezp_agent.requests, "get", fake_get)
    return calls


def test_run_once_sends_and_polls(cfg, monkeypatch):
    calls = _fake_world(monkeypatch, FakeResponse(200, {"code": 200, "data": {"requestCode": "req_7"}}))
    ezp_agent.run_once(cfg)

    assert ("POST-EZP", "http://127.0.0.1:3080/api/v2.3/letters", ["commonInfo", "letter.pdf", "letter.pdf.sig"]) in calls
    assert ("POST", "https://coop.example.ru/postal-letters/agent/jobs/7/result", {"request_code": "req_7"}) in calls
    assert ("GET-EZP", "http://127.0.0.1:3080/api/v2.3/letters/req_5/status", None) in calls
    assert ("POST", "https://coop.example.ru/postal-letters/agent/jobs/5/status",
            {"type": "ONLINE_DELIVERED", "mailId": "123"}) in calls
    journal = json.load(open(os.path.join(cfg.state_dir, "sent.json")))
    assert journal == {"https://coop.example.ru|gsk-7": "req_7"}


def test_already_sent_letter_is_not_sent_twice(cfg, monkeypatch):
    ezp_agent.SentJournal(cfg.state_dir).put("https://coop.example.ru|gsk-7", "req_old")
    calls = _fake_world(monkeypatch, FakeResponse(200, {"code": 200, "data": {"requestCode": "req_new"}}))
    ezp_agent.run_once(cfg)
    assert not any(c[0] == "POST-EZP" for c in calls)
    assert ("POST", "https://coop.example.ru/postal-letters/agent/jobs/7/result", {"request_code": "req_old"}) in calls


def test_rejected_letter_reported_as_error(cfg, monkeypatch):
    calls = _fake_world(monkeypatch, FakeResponse(400, {"code": 400, "message": "commonInfo невалидна"}))
    ezp_agent.run_once(cfg)
    result = [c for c in calls if c[1].endswith("/jobs/7/result")]
    assert result and "commonInfo невалидна" in result[0][2]["error"]


def test_forbidden_aborts_pass_without_failing_letter(cfg, monkeypatch):
    calls = _fake_world(monkeypatch, FakeResponse(403, {"code": 403, "message": "Доступ запрещен"}))
    with pytest.raises(ezp_agent.AgentError):
        ezp_agent.run_once(cfg)
    assert not any(c[1].endswith("/result") for c in calls)


def test_run_command_returns_error_code(cfg, monkeypatch):
    def boom(*a, **k):
        raise ezp_agent.requests.ConnectionError("down")
    monkeypatch.setattr(ezp_agent.requests, "request", boom)
    assert ezp_agent.cmd_run(cfg, loop=False) == 1
