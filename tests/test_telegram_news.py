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


def test_create_news_publishes_to_group(db, client, monkeypatch):
    _settings(db)
    sent = _capture_posts(monkeypatch)
    _login_board(db, client)

    resp = client.post("/news/new", data={"title": "Отключение <света>", "body": "Завтра с 10:00.", "publish_telegram": "1"})
    assert resp.status_code == 302

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


def test_telegram_failure_keeps_news_and_records_error(db, client, monkeypatch):
    from app.models import News
    settings = _settings(db)
    _capture_posts(monkeypatch, status_code=400)
    _login_board(db, client)

    resp = client.post("/news/new", data={"title": "Новость", "body": "Текст", "publish_telegram": "1"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert db.query(News).count() == 1
    assert "Не удалось отправить новость в Telegram" in resp.get_data(as_text=True)
    db.refresh(settings)
    assert "chat not found" in settings.last_error


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


def test_create_news_with_uploaded_photo_end_to_end(db, client, monkeypatch):
    from io import BytesIO
    _settings(db)
    sent = _capture_posts(monkeypatch)
    _login_board(db, client)
    client.post("/news/new", data={
        "title": "Субботник", "body": "Приходите!", "publish_telegram": "1",
        "attachments": [(BytesIO(b"JPEGDATA"), "photo.jpg")],
    }, content_type="multipart/form-data")
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
