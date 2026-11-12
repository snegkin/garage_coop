"""
Дублирование новостей в канал MAX (app/max_bot.py, очередь
app/messenger_outbox.py, настройки app/max_settings.py).
"""
import json as _json

import pytest
import requests as _rq

from app import max_bot, messenger_outbox
from app.bank_api import crypto
from app.models import MaxSettings, MessengerOutbox, MessengerOutboxStatus, RoleEnum, TelegramSettings
from app.news_format import max_html

from tests.conftest import make_user, login


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.text = _json.dumps(self._json)

    def json(self):
        return self._json


def _settings(db, news_chat_id="-70012345"):
    settings = MaxSettings(bot_token_encrypted=crypto.encrypt("max-token"), news_chat_id=news_chat_id)
    db.add(settings)
    db.commit()
    return settings


class FakeMax:
    """Подменяет requests в app/max_bot.py: API (requests.request) и
    загрузку файлов по выданному адресу (requests.post). script — ответы
    POST /messages по очереди (FakeResponse или исключение), дальше — 200."""

    def __init__(self, monkeypatch, script=()):
        self.calls = []
        self.messages = []
        self.uploads = []
        self.script = list(script)
        self.n = 0
        monkeypatch.setattr(max_bot.requests, "request", self.request)
        monkeypatch.setattr(max_bot.requests, "post", self.upload)
        monkeypatch.setattr(max_bot, "_sleep", lambda s: None)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=None):
        path = url.replace(max_bot.API_BASE, "")
        self.calls.append((method, path, params, json, headers))
        if path == "/uploads":
            self.n += 1
            return FakeResponse(200, {"url": f"https://upload.example/{params['type']}/{self.n}"})
        if path == "/messages":
            if self.script:
                result = self.script.pop(0)
                if isinstance(result, Exception):
                    raise result
                if result.status_code != 200:
                    return result
            self.messages.append({"params": params, "body": json})
            return FakeResponse(200, {"message": {"body": {"mid": "m1"}}})
        if path.startswith("/chats/"):
            return FakeResponse(200, {"title": "Канал кооператива"})
        return FakeResponse(200, {})

    def upload(self, url, files=None, headers=None, timeout=None, verify=None):
        name, fh = files["data"]
        self.uploads.append((url, name, fh.read()))
        kind = url.split("/")[-2]
        if kind == "image":
            return FakeResponse(200, {"photos": {"p1": {"token": f"img-{name}"}}})
        return FakeResponse(200, {"fileId": 1, "token": f"file-{name}"})


def _login_board(db, client):
    make_user(db, "max_news_board", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "max_news_board", "pass1234")


def _login_chair(db, client):
    make_user(db, "max_chair", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "max_chair", "pass1234")


def _process(app):
    return messenger_outbox.process(app.config["UPLOAD_FOLDER"])


def _news_with_files(app, db, files, body="Текст"):
    import os
    from app.models import News, NewsAttachment
    item = News(title="С вложениями", body=body)
    db.add(item)
    folder = app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    for n, name in enumerate(files):
        stored = f"maxtest_{n}_{name}"
        with open(os.path.join(folder, stored), "wb") as fh:
            fh.write(b"data")
        db.add(NewsAttachment(news=item, original_filename=name, stored_filename=stored, is_inline=False))
    db.commit()
    return item


def _enqueue(db, item, **kw):
    out = messenger_outbox.enqueue_news(item, "https://coop.example/news/1", channel="max", **kw)
    db.commit()
    return out


def test_max_html_drops_spoiler_keeps_formatting():
    html = max_html("**Важно**: пароль ||1234||", base_url="https://coop.example/news/1")
    assert "<strong>Важно</strong>" in html
    assert "1234" in html and "tg-spoiler" not in html


def test_create_news_publishes_to_max(app, db, client, monkeypatch):
    _settings(db)
    fake = FakeMax(monkeypatch)
    _login_board(db, client)
    resp = client.post("/news/new", data={"title": "Отключение <света>", "body": "Завтра.", "publish_max": "1"})
    assert resp.status_code == 302
    assert fake.messages == []  # уйдёт с cron
    out = db.query(MessengerOutbox).one()
    assert out.channel == "max"
    assert _process(app) == (1, 0)

    [msg] = fake.messages
    assert msg["params"]["chat_id"] == "-70012345"
    assert msg["body"]["format"] == "html" and msg["body"]["notify"] is True
    assert msg["body"]["text"].startswith("<b>Отключение &lt;света&gt;</b>")
    assert "/news/1" in msg["body"]["text"]
    assert fake.calls[0][4] == {"Authorization": "max-token"}


def test_images_go_with_text_files_separately(app, db, monkeypatch):
    _settings(db)
    fake = FakeMax(monkeypatch)
    item = _news_with_files(app, db, ["a.jpg", "b.png", "doc.pdf"])
    _enqueue(db, item, silent=True)
    assert _process(app) == (1, 0)

    first, second = fake.messages
    assert first["body"]["text"].startswith("<b>С вложениями</b>")
    assert first["body"]["attachments"] == [
        {"type": "image", "payload": {"token": "img-a.jpg"}},
        {"type": "image", "payload": {"token": "img-b.png"}},
    ]
    assert "text" not in second["body"]
    assert second["body"]["attachments"] == [{"type": "file", "payload": {"token": "file-doc.pdf"}}]
    assert first["body"]["notify"] is False and second["body"]["notify"] is False
    assert [name for _url, name, _data in fake.uploads] == ["a.jpg", "b.png", "doc.pdf"]


def test_more_than_twelve_images_split(app, db, monkeypatch):
    _settings(db)
    fake = FakeMax(monkeypatch)
    item = _news_with_files(app, db, [f"p{n}.jpg" for n in range(13)])
    _enqueue(db, item)
    _process(app)
    assert [len(m["body"]["attachments"]) for m in fake.messages] == [12, 1]
    assert "text" in fake.messages[0]["body"] and "text" not in fake.messages[1]["body"]


def test_attachment_not_ready_is_retried(app, db, monkeypatch):
    _settings(db)
    not_ready = FakeResponse(400, {"code": "attachment.not.ready", "message": "not processed"})
    fake = FakeMax(monkeypatch, script=[not_ready, not_ready])
    item = _news_with_files(app, db, ["a.jpg"])
    out = _enqueue(db, item)
    assert _process(app) == (1, 0)
    assert out.status == MessengerOutboxStatus.SENT and len(fake.messages) == 1


def test_attachment_never_ready_is_retried_later(app, db, monkeypatch):
    _settings(db)
    not_ready = FakeResponse(400, {"code": "attachment.not.ready", "message": "not processed"})
    FakeMax(monkeypatch, script=[not_ready] * 10)
    item = _news_with_files(app, db, ["a.jpg"])
    out = _enqueue(db, item)
    assert _process(app) == (0, 1)
    assert out.status == MessengerOutboxStatus.PENDING  # не отказ — попробуем ещё


def test_bad_request_fails_permanently(app, db, monkeypatch):
    _settings(db)
    FakeMax(monkeypatch, script=[FakeResponse(404, {"code": "chat.not.found", "message": "Chat not found"})])
    item = _news_with_files(app, db, [])
    out = _enqueue(db, item)
    _process(app)
    assert out.status == MessengerOutboxStatus.FAILED
    assert "chat.not.found" in out.last_error and "max-token" not in out.last_error


def test_read_timeout_is_uncertain(app, db, monkeypatch):
    _settings(db)
    FakeMax(monkeypatch, script=[_rq.ReadTimeout("read timed out")])
    item = _news_with_files(app, db, [])
    out = _enqueue(db, item)
    _process(app)
    assert out.status == MessengerOutboxStatus.UNCERTAIN


def test_max_outage_does_not_block_telegram(app, db, monkeypatch):
    from app import telegram_bot
    _settings(db)
    db.add(TelegramSettings(bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("123:ABC"),
                            news_chat_id="-100"))
    db.commit()
    FakeMax(monkeypatch, script=[_rq.ConnectTimeout("connect timeout")])
    tg_sent = []
    monkeypatch.setattr(telegram_bot.requests, "post",
                        lambda url, **kw: tg_sent.append(url) or FakeResponse(200, {"ok": True}))
    item = _news_with_files(app, db, [])
    max_out = _enqueue(db, item)
    tg_out = messenger_outbox.enqueue_news(item, "https://coop.example/news/1")
    db.commit()
    assert _process(app) == (1, 1)
    assert max_out.status == MessengerOutboxStatus.PENDING and max_out.attempts == 1
    assert tg_out.status == MessengerOutboxStatus.SENT and len(tg_sent) == 1


def test_unconfigured_channel_waits(app, db, monkeypatch):
    fake = FakeMax(monkeypatch)
    item = _news_with_files(app, db, [])
    out = _enqueue(db, item)
    assert _process(app) == (0, 0)
    assert out.status == MessengerOutboxStatus.PENDING and out.attempts == 0 and fake.calls == []


def test_form_and_menu_show_max_only_when_configured(app, db, client):
    from app.models import News
    db.add(News(title="Старая", body="Текст"))
    db.commit()
    _login_board(db, client)
    assert 'name="publish_max"' not in client.get("/news/new").get_data(as_text=True)
    assert "/news/1/max" not in client.get("/").get_data(as_text=True)
    _settings(db)
    form = client.get("/news/new").get_data(as_text=True)
    assert 'name="publish_max"' in form and 'name="max_silent"' in form
    assert 'name="publish_telegram"' not in form
    assert 'data-url="/news/1/max"' in client.get("/").get_data(as_text=True)


def test_send_existing_news_to_max(app, db, client, monkeypatch):
    from app.models import News
    _settings(db)
    item = News(title="Старая новость", body="Текст")
    db.add(item)
    db.commit()
    _login_board(db, client)
    fragment = client.get(f"/news/{item.id}/max", headers={"X-Requested-With": "XMLHttpRequest"}).get_data(as_text=True)
    assert 'id="maxSilent"' in fragment and "telegramOrderBlock" not in fragment and "<html" not in fragment
    assert client.get(f"/news/{item.id}/telegram").status_code == 302  # Telegram не настроен

    resp = client.post(f"/news/{item.id}/max", data={"max_silent": "1"})
    assert resp.status_code == 302
    out = db.query(MessengerOutbox).one()
    assert out.channel == "max" and out.silent

    again = client.get(f"/news/{item.id}/max", headers={"X-Requested-With": "XMLHttpRequest"}).get_data(as_text=True)
    assert "уже отправляли в MAX" in again


def test_poll_updates_remembers_chats(app, db, monkeypatch):
    settings = _settings(db)
    fake = FakeMax(monkeypatch)
    batches = [
        {"updates": [
            {"update_type": "bot_added", "chat_id": -555, "is_channel": True, "user": {"user_id": 1}},
            {"update_type": "message_created", "message": {"recipient": {"chat_id": -777, "chat_type": "chat"}}},
            {"update_type": "message_created", "message": {"recipient": {"user_id": 3, "chat_type": "dialog"}}},
        ], "marker": 42},
        {"updates": [{"update_type": "bot_removed", "chat_id": -777}], "marker": 43},
    ]
    original = fake.request

    def request(method, url, params=None, **kw):
        if url.endswith("/updates"):
            fake.calls.append((method, "/updates", params, None, None))
            return FakeResponse(200, batches.pop(0))
        return original(method, url, params=params, **kw)

    monkeypatch.setattr(max_bot.requests, "request", request)
    assert max_bot.poll_updates() == (3, 0)
    chats = max_bot.known_chats(settings)
    assert [(c["chat_id"], c["is_channel"], c["title"]) for c in chats] == [
        (-555, True, "Канал кооператива"), (-777, False, "Канал кооператива")]
    assert settings.last_marker == 42

    assert max_bot.poll_updates() == (1, 0)
    assert [c["chat_id"] for c in max_bot.known_chats(settings)] == [-555]
    update_calls = [c for c in fake.calls if c[1] == "/updates"]
    assert "marker" not in update_calls[0][2] and update_calls[1][2]["marker"] == 42


def test_settings_page_save_and_choose_chat(app, db, client):
    _login_chair(db, client)
    assert client.get("/max/").status_code == 200
    client.post("/max/settings", data={"bot_token": "secret-token", "news_chat_id": ""})
    settings = db.query(MaxSettings).one()
    assert crypto.decrypt(settings.bot_token_encrypted) == "secret-token"

    settings.known_chats = _json.dumps([{"chat_id": -555, "is_channel": True, "title": "Канал"}])
    db.commit()
    page = client.get("/max/").get_data(as_text=True)
    assert "Канал" in page and "Новости сюда" in page and "secret-token" not in page

    client.post("/max/settings", data={"news_chat_id": "-555"})
    db.refresh(settings)
    assert settings.news_chat_id == "-555"
    assert crypto.decrypt(settings.bot_token_encrypted) == "secret-token"  # пустое поле токена не стирает

    client.post("/max/settings", data={"news_chat_id": "@канал"})
    db.refresh(settings)
    assert settings.news_chat_id == "-555"


def test_settings_require_chairman(db, client):
    _login_board(db, client)
    assert client.get("/max/").status_code in (302, 403)


def test_outbox_actions_only_for_max_items(app, db, client):
    from app.models import News
    item = News(title="Н", body="Т")
    db.add(item)
    db.commit()
    tg_out = messenger_outbox.enqueue_news(item, "https://coop.example/news/1")
    max_out = _enqueue(db, item)
    max_out.status = MessengerOutboxStatus.FAILED
    db.commit()
    _login_chair(db, client)
    assert client.post(f"/max/outbox/{tg_out.id}/cancel").status_code == 404
    assert "Н" in client.get("/max/").get_data(as_text=True)
    client.post(f"/max/outbox/{max_out.id}/retry")
    db.refresh(max_out)
    assert max_out.status == MessengerOutboxStatus.PENDING


def test_verify_adds_russian_root_to_certifi(app, tmp_path):
    import certifi
    root = tmp_path / "root.pem"
    root.write_bytes(b"-----RUSSIAN ROOT-----\n")
    app.config["MAX_API_CA_BUNDLE"] = str(root)
    with app.app_context():
        path = max_bot._verify()
    data = open(path, "rb").read()
    assert data.startswith(open(certifi.where(), "rb").read()) and b"RUSSIAN ROOT" in data

    app.config["MAX_API_CA_BUNDLE"] = str(tmp_path / "missing.pem")
    with app.app_context():
        assert max_bot._verify() is True
