"""
Дублирование новостей сайта в группу Telegram (app/telegram_bot.py:
publish_news, app/news.py: create) и подсказка ID группы по /chatid.
"""
from app import telegram_bot
from app.bank_api import crypto
from app.models import RoleEnum, TelegramSettings
from app.news_format import telegram_html

from tests.conftest import make_user, login


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {"ok": True}
        self.text = text

    def json(self):
        return self._json


def _settings(db, news_chat_id="-1001234567890"):
    settings = TelegramSettings(
        bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("123:ABC"), news_chat_id=news_chat_id,
    )
    db.add(settings)
    db.commit()
    return settings


def _capture_posts(monkeypatch, status_code=200):
    sent = []

    def fake_post(url, json=None, data=None, files=None, timeout=None, proxies=None):
        sent.append({"proxies": proxies,
            "url": url, "method": url.rsplit("/", 1)[-1], "json": json, "data": data,
            "files": {k: (name, fh.read()) for k, (name, fh) in (files or {}).items()},
        })
        return FakeResponse(status_code, text="Bad Request: chat not found")

    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)
    return sent


def _login_board(db, client):
    make_user(db, "tg_news_board", "pass1234", role=RoleEnum.BOARD)
    db.commit()
    login(client, "tg_news_board", "pass1234")


def test_telegram_html_converts_markdown():
    html = telegram_html(
        "### Собрание\n\n**Важно**: взнос *до* 1 декабря.\n\n- раз\n- два\n\n"
        "Пароль ||1234||, [файл](/news/attachments/5/a.pdf) ![](/x.png) <script>x</script>",
        base_url="https://coop.example/news/7",
    )
    assert "<b>Собрание</b>" in html
    assert "<strong>Важно</strong>" in html and "<em>до</em>" in html
    assert "• раз\n• два" in html
    assert '<span class="tg-spoiler">1234</span>' in html
    assert 'href="https://coop.example/news/attachments/5/a.pdf"' in html
    for tag in ("<p>", "<h3>", "<img", "<ul>", "<li>", "<script"):
        assert tag not in html


def _process(app):
    from app import telegram_outbox
    return telegram_outbox.process(app.config["UPLOAD_FOLDER"])


def test_create_news_publishes_to_group(app, db, client, monkeypatch):
    _settings(db)
    sent = _capture_posts(monkeypatch)
    _login_board(db, client)

    resp = client.post("/news/new", data={"title": "Отключение <света>", "body": "Завтра с 10:00.", "publish_telegram": "1"})
    assert resp.status_code == 302
    assert sent == []  # сохранение новости не ждёт Telegram — уйдёт с cron
    assert _process(app) == (1, 0)

    assert len(sent) == 1
    payload = sent[0]["json"]
    assert sent[0]["url"].endswith("/sendMessage")
    assert payload["chat_id"] == "-1001234567890"
    assert payload["parse_mode"] == "HTML"
    assert payload["text"].startswith("<b>Отключение &lt;света&gt;</b>")
    assert "Завтра с 10:00." in payload["text"]
    assert "/news/1" in payload["text"]


def test_create_news_without_checkbox_does_not_publish(db, client, monkeypatch):
    _settings(db)
    sent = _capture_posts(monkeypatch)
    _login_board(db, client)

    client.post("/news/new", data={"title": "Тихо", "body": "Текст"})
    assert sent == []


def test_form_shows_checkbox_only_when_group_configured(db, client):
    _login_board(db, client)
    assert "publish_telegram" not in client.get("/news/new").get_data(as_text=True)
    _settings(db)
    assert "publish_telegram" in client.get("/news/new").get_data(as_text=True)


def test_long_news_falls_back_to_excerpt(app, db, monkeypatch):
    from app.models import News
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = News(title="Длинная", body="слово " * 2000)
    telegram_bot.publish_news(settings, item, "https://coop.example/news/1", app.config["UPLOAD_FOLDER"])
    text = sent[0]["json"]["text"]
    assert len(text) < 4096
    assert "…" in text


def test_chatid_command_replies_with_chat_id(app, db, monkeypatch):
    settings = _settings(db, news_chat_id=None)

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(200, {"ok": True, "result": [
            {"update_id": 3, "message": {"text": "/chatid@coop_bot", "chat": {"id": -100555, "type": "supergroup"}}},
            {"update_id": 4, "channel_post": {"text": "/chatid", "chat": {"id": -100777, "type": "channel"}}},
        ]})

    monkeypatch.setattr(telegram_bot.requests, "get", fake_get)
    sent = _capture_posts(monkeypatch)

    total, linked = telegram_bot.poll_and_link_accounts()
    assert (total, linked) == (2, 0)
    assert [s["json"]["chat_id"] for s in sent] == [-100555, -100777]
    assert "-100555" in sent[0]["json"]["text"]
    db.refresh(settings)
    assert settings.last_update_id == 4


def test_chairman_saves_news_chat_id(db, client):
    _settings(db, news_chat_id=None)
    make_user(db, "tg_chair", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "tg_chair", "pass1234")
    client.post("/telegram/settings", data={"bot_username": "coop_bot", "bot_token": "", "news_chat_id": " @coop_news "})
    settings = db.query(TelegramSettings).first()
    db.refresh(settings)
    assert settings.news_chat_id == "@coop_news"


# ---------------------------------------------------------------------------
# Картинки и файлы
# ---------------------------------------------------------------------------

def _news_with_files(app, db, body, files):
    """files — [(имя, байты, is_inline)]; файлы кладутся прямо в UPLOAD_FOLDER."""
    import os
    from app.models import News, NewsAttachment
    item = News(title="С вложениями", body=body)
    db.add(item)
    folder = app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    for n, (name, content, inline) in enumerate(files):
        stored = f"tgtest_{n}_{name}"
        with open(os.path.join(folder, stored), "wb") as fh:
            fh.write(content)
        db.add(NewsAttachment(news=item, original_filename=name, stored_filename=stored, is_inline=inline))
    db.commit()
    return item


def _publish(app, settings, item, **kw):
    return telegram_bot.publish_news(settings, item, "https://coop.example/news/1", app.config["UPLOAD_FOLDER"], **kw)


def test_short_text_with_photos_is_one_album_post(app, db, monkeypatch):
    import json
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "Короткий текст\n\n![](/news/attachments/1/a.jpg)", [
        ("a.jpg", b"A", True), ("b.png", b"B", False), ("doc.pdf", b"PDF", False),
    ])
    assert _publish(app, settings, item) == []

    assert [s["method"] for s in sent] == ["sendMediaGroup", "sendDocument"]
    media = json.loads(sent[0]["data"]["media"])
    assert [m["media"] for m in media] == ["attach://file0", "attach://file1"]
    assert "Короткий текст" in media[0]["caption"] and media[0]["parse_mode"] == "HTML"
    assert "caption" not in media[1]
    assert sent[0]["files"] == {"file0": ("a.jpg", b"A"), "file1": ("b.png", b"B")}
    assert sent[1]["files"]["document"] == ("doc.pdf", b"PDF")


def test_single_photo_uses_send_photo_with_caption(app, db, monkeypatch):
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "Текст", [("a.jpg", b"A", False)])
    _publish(app, settings, item)
    assert [s["method"] for s in sent] == ["sendPhoto"]
    assert "Текст" in sent[0]["data"]["caption"]


def test_long_text_with_photos_text_first_by_default(app, db, monkeypatch):
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "длинно " * 300, [("a.jpg", b"A", False), ("b.jpg", b"B", False)])
    _publish(app, settings, item)
    assert [s["method"] for s in sent] == ["sendMessage", "sendMediaGroup"]
    assert "caption" not in sent[1]["data"]["media"]


def test_long_text_with_photos_album_first(app, db, monkeypatch):
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "длинно " * 300, [("a.jpg", b"A", False), ("b.jpg", b"B", False)])
    _publish(app, settings, item, album_first=True)
    assert [s["method"] for s in sent] == ["sendMediaGroup", "sendMessage"]


def test_more_than_ten_photos_split_into_albums(app, db, monkeypatch):
    import json
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "Фотоотчёт", [(f"p{n}.jpg", b"x", False) for n in range(11)])
    _publish(app, settings, item)
    assert [s["method"] for s in sent] == ["sendMediaGroup", "sendPhoto"]
    assert len(json.loads(sent[0]["data"]["media"])) == 10
    assert "caption" not in sent[1]["data"]


def test_oversized_files(app, db, monkeypatch):
    """Фото больше 10 МБ уходит документом, файл больше 50 МБ — пропускается."""
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    monkeypatch.setattr(telegram_bot, "PHOTO_MAX_BYTES", 3)
    monkeypatch.setattr(telegram_bot, "DOCUMENT_MAX_BYTES", 5)
    item = _news_with_files(app, db, "Текст", [("big.jpg", b"1234", False), ("huge.pdf", b"123456", False)])
    assert _publish(app, settings, item) == ["huge.pdf"]
    assert [s["method"] for s in sent] == ["sendMessage", "sendDocument"]
    assert sent[1]["files"]["document"][0] == "big.jpg"


def test_create_news_with_uploaded_photo_end_to_end(app, db, client, monkeypatch):
    from io import BytesIO
    _settings(db)
    sent = _capture_posts(monkeypatch)
    _login_board(db, client)
    client.post("/news/new", data={
        "title": "Субботник", "body": "Приходите!", "publish_telegram": "1",
        "attachments": [(BytesIO(b"JPEGDATA"), "photo.jpg")],
    }, content_type="multipart/form-data")
    _process(app)
    assert [s["method"] for s in sent] == ["sendPhoto"]
    assert sent[0]["files"]["photo"] == ("photo.jpg", b"JPEGDATA")
    assert "Субботник" in sent[0]["data"]["caption"]


def test_telegram_layout_endpoint(db, client):
    _login_board(db, client)
    short = client.post("/news/telegram-layout", data={"title": "Т", "body": "коротко"}).get_json()
    long = client.post("/news/telegram-layout", data={"title": "Т", "body": "длинно " * 300}).get_json()
    assert short == {"fits_caption": True}
    assert long == {"fits_caption": False}


def test_caption_length_counts_utf16_and_ignores_tags():
    assert telegram_bot._visible_length("<b>a&amp;b</b>😀") == 5


def test_connection_error_does_not_leak_token(app, db, monkeypatch):
    import pytest
    import requests
    from app import progress
    settings = _settings(db)

    def boom(url, **kw):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url.split('api.telegram.org')[1]} "
                                       "[Errno 101] Network is unreachable")

    monkeypatch.setattr(telegram_bot.requests, "get", boom)
    with pytest.raises(telegram_bot.TelegramError) as info:
        telegram_bot.get_me(settings)
    assert "123:ABC" not in str(info.value)
    assert "/bot***/getMe" in str(info.value)

    text = progress._connection_error_text(requests.ConnectionError("url: /bot123:ABC/getMe Network is unreachable"))
    assert "123:ABC" not in text
    assert "нет маршрута" in text
    assert "123:ABC" not in progress._exc_text(Exception("/bot123:ABC/getMe"))



# ---------------------------------------------------------------------------
# Обход блокировки: свой адрес Bot API и прокси
# ---------------------------------------------------------------------------

def test_custom_api_base_and_proxy_are_used(app, db, monkeypatch):
    settings = _settings(db)
    settings.api_base_url = "https://tg-relay.example/"
    settings.proxy_url_encrypted = crypto.encrypt("socks5h://u:p@proxy.example:1080")
    db.commit()
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "Текст", [("a.jpg", b"A", False), ("doc.pdf", b"D", False)])
    _publish(app, settings, item)
    assert sent[0]["url"] == "https://tg-relay.example/bot123:ABC/sendPhoto"
    proxy = {"http": "socks5h://u:p@proxy.example:1080", "https": "socks5h://u:p@proxy.example:1080"}
    assert all(s["proxies"] == proxy for s in sent)


def test_get_requests_use_proxy(app, db, monkeypatch):
    settings = _settings(db)
    settings.proxy_url_encrypted = crypto.encrypt("http://proxy.example:3128")
    db.commit()
    calls = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append((url, proxies))
        return FakeResponse(200, {"ok": True, "result": {"username": "coop_bot"}})

    monkeypatch.setattr(telegram_bot.requests, "get", fake_get)
    telegram_bot.get_me(settings)
    assert calls == [("https://api.telegram.org/bot123:ABC/getMe",
                      {"http": "http://proxy.example:3128", "https": "http://proxy.example:3128"})]


def _login_chair(db, client):
    make_user(db, "tg_chair2", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "tg_chair2", "pass1234")


def _save(client, **fields):
    data = {"bot_username": "coop_bot", "bot_token": "", **fields}
    return client.post("/telegram/settings", data=data, follow_redirects=True)


def test_save_api_base_and_proxy(db, client):
    _settings(db)
    _login_chair(db, client)
    _save(client, api_base_url="https://tg-relay.example/", proxy_url="socks5h://u:p@h:1080")
    settings = db.query(TelegramSettings).first()
    db.refresh(settings)
    assert settings.api_base_url == "https://tg-relay.example"
    assert crypto.decrypt(settings.proxy_url_encrypted) == "socks5h://u:p@h:1080"

    page = client.get("/telegram/").get_data(as_text=True)
    assert "u:p@h" not in page  # пароль прокси в форму не возвращается

    _save(client, api_base_url="https://tg-relay.example")  # пустое поле — прокси прежний
    db.refresh(settings)
    assert settings.proxy_url_encrypted

    _save(client, proxy_clear="1")
    db.refresh(settings)
    assert settings.proxy_url_encrypted is None
    assert settings.api_base_url is None


def test_save_rejects_bad_proxy_and_url(db, client):
    _settings(db)
    _login_chair(db, client)
    resp = _save(client, proxy_url="proxy.example:1080")
    assert "socks5h://" in resp.get_data(as_text=True)
    resp = _save(client, api_base_url="tg-relay.example")
    assert "должен начинаться с https://" in resp.get_data(as_text=True)
    settings = db.query(TelegramSettings).first()
    db.refresh(settings)
    assert settings.proxy_url_encrypted is None and settings.api_base_url is None



# ---------------------------------------------------------------------------
# Очередь отправки: повторы, продолжение с места сбоя, неизвестный исход
# ---------------------------------------------------------------------------

def _scripted_posts(monkeypatch, script):
    """script — список исходов по очереди запросов: "ok", HTTP-код (int) или
    исключение requests; после конца списка — всё "ok"."""
    import requests as _rq
    sent = []
    outcomes = list(script)

    def fake_post(url, json=None, data=None, files=None, timeout=None, proxies=None):
        method = url.rsplit("/", 1)[-1]
        outcome = outcomes.pop(0) if outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        sent.append(method)
        if outcome == "ok":
            return FakeResponse(200)
        return FakeResponse(outcome, text="error")

    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)
    return sent


def _queued_news(app, db, files=(), body="Текст"):
    from app import telegram_outbox
    item = _news_with_files(app, db, body, list(files))
    out = telegram_outbox.enqueue_news(item, "https://coop.example/news/1")
    db.commit()
    return out


def test_outbox_retries_after_network_error_with_backoff(app, db, monkeypatch):
    import datetime as dt
    import requests as _rq
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    _scripted_posts(monkeypatch, [_rq.ConnectionError("Failed to establish a new connection: Network is unreachable")])

    assert _process(app) == (0, 1)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.PENDING
    assert out.next_attempt_at > dt.datetime.utcnow()
    assert "Network is unreachable" in out.last_error

    assert _process(app) == (0, 0)  # срок повтора ещё не подошёл
    later = dt.datetime.utcnow() + dt.timedelta(minutes=2)
    assert telegram_outbox.process(app.config["UPLOAD_FOLDER"], now=later) == (1, 0)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.SENT


def test_outbox_network_error_stops_batch(app, db, monkeypatch):
    import requests as _rq
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    telegram_outbox.enqueue_message(1, "раз")
    telegram_outbox.enqueue_message(2, "два")
    db.commit()
    sent = _scripted_posts(monkeypatch, [_rq.ConnectTimeout("connect timeout")])
    assert _process(app) == (0, 1)
    assert sent == []  # второе даже не пробовали — сеть лежит


def test_outbox_resumes_news_from_failed_step(app, db, monkeypatch):
    """Длинный текст + 2 фото + документ = 3 шага; сбой на втором — повтор
    не отправляет текст второй раз."""
    import datetime as dt
    import requests as _rq
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db, body="длинно " * 300,
                       files=[("a.jpg", b"A", False), ("b.jpg", b"B", False), ("d.pdf", b"D", False)])
    sent = _scripted_posts(monkeypatch, ["ok", _rq.ConnectionError("NewConnectionError: refused")])

    _process(app)
    db.refresh(out)
    assert out.steps_done == 1 and out.status == TelegramOutboxStatus.PENDING

    later = dt.datetime.utcnow() + dt.timedelta(minutes=2)
    telegram_outbox.process(app.config["UPLOAD_FOLDER"], now=later)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.SENT
    assert sent == ["sendMessage", "sendMediaGroup", "sendDocument"]


def test_outbox_read_timeout_is_uncertain_not_retried(app, db, monkeypatch):
    import datetime as dt
    import requests as _rq
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    sent = _scripted_posts(monkeypatch, [_rq.ReadTimeout("read timed out")])
    _process(app)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.UNCERTAIN
    later = dt.datetime.utcnow() + dt.timedelta(hours=2)
    assert telegram_outbox.process(app.config["UPLOAD_FOLDER"], now=later) == (0, 0)
    assert sent == []


def test_outbox_bad_request_fails_permanently(app, db, monkeypatch):
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    _scripted_posts(monkeypatch, [400])
    _process(app)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.FAILED


def test_outbox_rate_limit_is_retried(app, db, monkeypatch):
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    _scripted_posts(monkeypatch, [429])
    _process(app)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.PENDING


def test_outbox_gives_up_after_max_age(app, db, monkeypatch):
    import datetime as dt
    import requests as _rq
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    _scripted_posts(monkeypatch, [_rq.ConnectTimeout("x")])
    later = dt.datetime.utcnow() + telegram_outbox.MAX_AGE + dt.timedelta(minutes=1)
    telegram_outbox.process(app.config["UPLOAD_FOLDER"], now=later)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.FAILED


def test_outbox_page_and_actions(app, db, client, monkeypatch):
    import requests as _rq
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    _scripted_posts(monkeypatch, [_rq.ReadTimeout("read timed out")])
    _process(app)

    make_user(db, "tg_chair_q", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "tg_chair_q", "pass1234")
    page = client.get("/telegram/").get_data(as_text=True)
    assert "С вложениями" in page and "неизвестно" in page

    client.post(f"/telegram/outbox/{out.id}/mark-sent")
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.SENT

    out.status = TelegramOutboxStatus.FAILED
    db.commit()
    client.post(f"/telegram/outbox/{out.id}/retry")
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.PENDING
    client.post(f"/telegram/outbox/{out.id}/cancel")
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.CANCELLED
    assert client.post(f"/telegram/outbox/{out.id}/explode").status_code == 404


def test_outbox_deleted_news_fails(app, db, monkeypatch):
    from app.models import TelegramOutboxStatus
    _settings(db)
    out = _queued_news(app, db)
    db.delete(out.news)
    db.commit()
    _scripted_posts(monkeypatch, [])
    _process(app)
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.FAILED


# ---------------------------------------------------------------------------
# Тихая и отложенная отправка
# ---------------------------------------------------------------------------

def test_silent_news_sets_disable_notification_everywhere(app, db, monkeypatch):
    import json as _json
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "длинно " * 300,
                            [("a.jpg", b"A", False), ("b.jpg", b"B", False), ("d.pdf", b"D", False)])
    _publish(app, settings, item, silent=True)
    assert [s["method"] for s in sent] == ["sendMessage", "sendMediaGroup", "sendDocument"]
    assert sent[0]["json"]["disable_notification"] is True
    assert sent[1]["data"]["disable_notification"] == "true"
    assert sent[2]["data"]["disable_notification"] == "true"


def test_not_silent_by_default(app, db, monkeypatch):
    settings = _settings(db)
    sent = _capture_posts(monkeypatch)
    item = _news_with_files(app, db, "Текст", [("a.jpg", b"A", False)])
    _publish(app, settings, item)
    assert "disable_notification" not in sent[0]["data"]


def test_create_news_scheduled_and_silent(app, db, client, monkeypatch):
    import datetime as dt
    from app import telegram_outbox
    from app.models import TelegramOutbox, TelegramOutboxStatus
    _settings(db)
    sent = _capture_posts(monkeypatch)
    _login_board(db, client)
    when = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(hours=3)

    resp = client.post("/news/new", data={
        "title": "Собрание", "body": "В субботу", "publish_telegram": "1",
        "telegram_silent": "1", "telegram_send_at_utc": when.isoformat() + ".000Z",
    }, follow_redirects=True)
    assert "запланирован" in resp.get_data(as_text=True)
    out = db.query(TelegramOutbox).one()
    assert out.silent and out.scheduled_at == when and out.next_attempt_at == when

    assert _process(app) == (0, 0)
    assert sent == []
    # Даже через сутки после постановки (но вскоре после срока) — не «просрочено»
    telegram_outbox.process(app.config["UPLOAD_FOLDER"], now=when + dt.timedelta(minutes=1))
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.SENT
    assert sent[0]["json"]["disable_notification"] is True


def test_scheduled_in_past_or_garbage_sends_now(app, db, client, monkeypatch):
    from app.models import TelegramOutbox
    _settings(db)
    _capture_posts(monkeypatch)
    _login_board(db, client)
    client.post("/news/new", data={"title": "А", "body": "Б", "publish_telegram": "1",
                                   "telegram_send_at_utc": "2001-01-01T00:00:00.000Z"})
    client.post("/news/new", data={"title": "В", "body": "Г", "publish_telegram": "1",
                                   "telegram_send_at_utc": "не дата"})
    assert [o.scheduled_at for o in db.query(TelegramOutbox).all()] == [None, None]
    assert _process(app) == (2, 0)


def test_max_age_counts_from_scheduled_time(app, db, monkeypatch):
    import datetime as dt
    import requests as _rq
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    item = _news_with_files(app, db, "Текст", [])
    when = dt.datetime.utcnow() + dt.timedelta(days=3)
    out = telegram_outbox.enqueue_news(item, "https://coop.example/news/1", send_at=when)
    db.commit()
    _scripted_posts(monkeypatch, [_rq.ConnectTimeout("x")])
    telegram_outbox.process(app.config["UPLOAD_FOLDER"], now=when + dt.timedelta(minutes=1))
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.PENDING  # не отказ: срок только наступил


def test_scheduled_shown_and_cancellable_on_settings_page(app, db, client, monkeypatch):
    import datetime as dt
    from app import telegram_outbox
    from app.models import TelegramOutboxStatus
    _settings(db)
    item = _news_with_files(app, db, "Текст", [])
    out = telegram_outbox.enqueue_news(item, "https://coop.example/news/1", silent=True,
                                       send_at=dt.datetime.utcnow() + dt.timedelta(hours=5))
    db.commit()
    make_user(db, "tg_chair_s", "pass1234", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "tg_chair_s", "pass1234")
    page = client.get("/telegram/").get_data(as_text=True)
    assert "запланировано" in page and "bi-bell-slash" in page
    client.post(f"/telegram/outbox/{out.id}/cancel")
    db.refresh(out)
    assert out.status == TelegramOutboxStatus.CANCELLED


# ---------------------------------------------------------------------------
# Сохранение форматирования
# ---------------------------------------------------------------------------

def test_telegram_html_lists_numbered_nested_and_spacing():
    html = telegram_html(
        "Шаги:\n\n1. раз\n2. **два**\n\nМаркеры:\n\n- а\n- б\n    - вложенный\n- в\n\nКонец",
        base_url="https://coop.example/",
    )
    assert "1. раз\n2. <strong>два</strong>\n\nМаркеры:" in html
    assert "• а\n• б\n    • вложенный\n• в\n\nКонец" in html


def test_telegram_html_drops_unsafe_links_and_script_content():
    html = telegram_html("[плохая](javascript:alert(1)) <script>alert(2)</script>текст", base_url="https://a.b/")
    assert "javascript" not in html and "alert" not in html
    assert "плохая" in html and "текст" in html


def test_telegram_html_keeps_code_block_newlines():
    html = telegram_html("Код:\n\n    строка1\n      строка2", base_url="https://a.b/")
    assert "<pre><code>строка1\n  строка2" in html


def test_long_news_truncated_by_paragraphs_keeps_formatting():
    body = "\n\n".join(f"Абзац **{n}** " + "слово " * 60 for n in range(40))
    text = telegram_bot.news_text("Длинная", body, "https://coop.example/news/1")
    assert telegram_bot._visible_length(text) < 4096
    assert "<strong>0</strong>" in text  # разметка сохранилась
    assert "…" in text and "Читать на сайте" in text
    assert telegram_bot._balanced(text)


def test_truncation_does_not_cut_inside_quote():
    quote = "> " + "\n>\n> ".join("цитата " * 40 for _ in range(30))
    body = "Вступление\n\n" + quote
    text = telegram_bot.news_text("Т", body, "https://coop.example/news/1")
    assert telegram_bot._balanced(text)
    assert telegram_bot._visible_length(text) < 4096


# ---------------------------------------------------------------------------
# Отправка уже существующей новости из меню
# ---------------------------------------------------------------------------

def test_menu_item_shown_only_when_group_configured(app, db, client):
    from app.models import News
    db.add(News(title="Старая", body="Текст"))
    db.commit()
    _login_board(db, client)
    assert "/news/1/telegram" not in client.get("/").get_data(as_text=True)
    _settings(db)
    assert "/news/1/telegram" in client.get("/").get_data(as_text=True)


def test_send_existing_news_with_options(app, db, client, monkeypatch):
    import datetime as dt
    from app.models import News, TelegramOutbox
    _settings(db)
    sent = _capture_posts(monkeypatch)
    item = News(title="Старая новость", body="Текст")
    db.add(item)
    db.commit()
    _login_board(db, client)

    page = client.get(f"/news/{item.id}/telegram").get_data(as_text=True)
    assert "Старая новость" in page and "telegram_silent" in page
    assert "уже отправляли" not in page

    resp = client.post(f"/news/{item.id}/telegram", data={"telegram_silent": "1"}, follow_redirects=True)
    assert "в очереди" in resp.get_data(as_text=True) or "поставлена в очередь" in resp.get_data(as_text=True)
    out = db.query(TelegramOutbox).one()
    assert out.news_id == item.id and out.silent
    _process(app)
    assert sent[0]["json"]["disable_notification"] is True

    page = client.get(f"/news/{item.id}/telegram").get_data(as_text=True)
    assert "уже отправляли" in page and "отправлена" in page


def test_send_existing_news_scheduled(app, db, client, monkeypatch):
    import datetime as dt
    from app.models import News, TelegramOutbox
    _settings(db)
    item = News(title="Н", body="Т")
    db.add(item)
    db.commit()
    _login_board(db, client)
    when = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(days=1)
    client.post(f"/news/{item.id}/telegram", data={"telegram_send_at_utc": when.isoformat() + "Z"})
    assert db.query(TelegramOutbox).one().scheduled_at == when
    assert "запланирована" in client.get(f"/news/{item.id}/telegram").get_data(as_text=True)


def test_send_existing_news_order_switch_shown_for_long_text_with_photos(app, db, client):
    _settings(db)
    item = _news_with_files(app, db, "длинно " * 300, [("a.jpg", b"A", False)])
    _login_board(db, client)
    page = client.get(f"/news/{item.id}/telegram").get_data(as_text=True)
    assert 'mb-3 ms-4 " id="telegramOrderBlock"' in page


def test_send_existing_news_requires_group_and_board(app, db, client):
    from app.models import News
    item = News(title="Н", body="Т")
    db.add(item)
    db.commit()
    _login_board(db, client)
    resp = client.get(f"/news/{item.id}/telegram")
    assert resp.status_code == 302  # группа не настроена
    assert client.get("/news/999/telegram").status_code in (302, 404)

    _settings(db)
    client.get("/logout")
    make_user(db, "tg_member", "pass1234", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "tg_member", "pass1234")
    assert client.post(f"/news/{item.id}/telegram").status_code in (302, 403)
    from app.models import TelegramOutbox
    assert db.query(TelegramOutbox).count() == 0


def test_send_telegram_modal_and_fragment(app, db, client):
    from app.models import News
    _settings(db)
    item = News(title="Старая новость", body="Текст")
    db.add(item)
    db.commit()
    _login_board(db, client)
    home = client.get("/").get_data(as_text=True)
    assert 'id="telegramSendModal"' in home
    assert f'data-url="/news/{item.id}/telegram"' in home

    fragment = client.get(f"/news/{item.id}/telegram", headers={"X-Requested-With": "XMLHttpRequest"}).get_data(as_text=True)
    assert "Старая новость" in fragment and 'id="telegramSilent"' in fragment
    assert "<html" not in fragment
