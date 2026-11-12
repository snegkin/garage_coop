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

    def fake_post(url, json=None, timeout=None):
        sent.append({"url": url, "json": json})
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
    telegram_bot.publish_news(settings, item, "https://coop.example/news/1")
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
