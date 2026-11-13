"""
Мост чата правления с закрытыми группами правления в Telegram и MAX
(app/board_chat.py: relay_to_messengers / ingest_external,
TelegramSettings/MaxSettings.board_chat_id): сообщение с сайта уходит в
очередь обеих групп, сообщение из группы появляется на сайте и уходит
только в другую группу.
"""
from app import max_bot, messenger_outbox, telegram_bot
from app.bank_api import crypto
from app.models import BoardChatMessage, MaxSettings, MessengerOutbox, RoleEnum, TelegramSettings

from tests.conftest import make_person, make_user, login
from tests.test_max_news import FakeMax
from tests.test_max_notifications import _updates as _max_updates
from tests.test_telegram_notifications import FakeResponse

TG_GROUP = "-1009990001"
MAX_GROUP = "-777"


def _bridges(db, telegram=True, max_=True):
    if telegram:
        db.add(TelegramSettings(bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("1:A"),
                                board_chat_id=TG_GROUP))
    if max_:
        db.add(MaxSettings(bot_token_encrypted=crypto.encrypt("max-token"), board_chat_id=MAX_GROUP))
    db.commit()


def _board_user(db, username="board1", **person_kwargs):
    person = make_person(db, full_name="Board One", **person_kwargs)
    user = make_user(db, username, "pass1234", role=RoleEnum.BOARD, person=person)
    db.commit()
    return user


def _outbox(db):
    return sorted(((i.channel, i.kind, i.chat_id, i.text) for i in db.query(MessengerOutbox)))


def _tg_updates(monkeypatch, messages):
    result = [{"update_id": n + 1, "message": m} for n, m in enumerate(messages)]

    def fake_get(url, params=None, timeout=None, **kw):
        return FakeResponse(200, {"ok": True, "result": result})

    monkeypatch.setattr(telegram_bot.requests, "get", fake_get)
    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **kw: FakeResponse(200))


def _tg_message(text, message_id=10, from_id=5001, chat_id=TG_GROUP, **extra):
    return {"message_id": message_id, "chat": {"id": int(chat_id), "type": "supergroup"},
            "from": {"id": from_id, "first_name": "Пётр", "last_name": "Иванов"}, "text": text, **extra}


# ---------------------------------------------------------------------------
# Сайт -> мессенджеры
# ---------------------------------------------------------------------------

def test_site_message_queued_to_both_groups(db, client):
    _bridges(db)
    _board_user(db)
    login(client, "board1", "pass1234")

    resp = client.post("/board-chat/messages", data={"body": "привет"})
    assert resp.status_code == 200
    assert resp.get_json()["message"]["source"] == "site"
    assert _outbox(db) == [
        ("max", "group", MAX_GROUP, "board1: привет"),
        ("telegram", "group", TG_GROUP, "board1: привет"),
    ]


def test_no_bridge_no_queue(db, client):
    db.add(TelegramSettings(bot_username="coop_bot", bot_token_encrypted=crypto.encrypt("1:A")))
    db.commit()
    _board_user(db)
    login(client, "board1", "pass1234")

    client.post("/board-chat/messages", data={"body": "привет"})
    assert db.query(MessengerOutbox).count() == 0


def test_group_outbox_sends_to_chat(app, db, monkeypatch):
    """Очередь kind="group": Telegram — sendMessage в группу, MAX — chat_id, не user_id."""
    _bridges(db)
    messenger_outbox.enqueue_group_message(TG_GROUP, "a: b", channel="telegram")
    messenger_outbox.enqueue_group_message(MAX_GROUP, "a: b", channel="max")
    db.commit()
    # requests у обоих ботов — один модуль: FakeMax подменяет requests.post
    # под загрузку файлов, поэтому Telegram-подмену ставим после него.
    fake = FakeMax(monkeypatch)
    sent_tg = []
    monkeypatch.setattr(telegram_bot.requests, "post", lambda url, json=None, **kw: sent_tg.append(json) or FakeResponse(200))

    assert messenger_outbox.process(app.config["UPLOAD_FOLDER"]) == (2, 0)
    assert sent_tg == [{"chat_id": TG_GROUP, "text": "a: b"}]
    assert fake.messages[0]["params"] == {"chat_id": MAX_GROUP}


# ---------------------------------------------------------------------------
# Telegram -> сайт
# ---------------------------------------------------------------------------

def test_telegram_message_from_linked_board_member(app, db, monkeypatch):
    _bridges(db)
    user = _board_user(db, telegram_chat_id=5001)
    _tg_updates(monkeypatch, [_tg_message("из телеги")])

    telegram_bot.poll_and_link_accounts()

    message = db.query(BoardChatMessage).one()
    assert (message.author_id, message.source, message.body) == (user.id, "telegram", "из телеги")
    # только в MAX — не обратно в Telegram
    assert _outbox(db) == [("max", "group", MAX_GROUP, "board1: из телеги")]


def test_telegram_message_from_unlinked_person(app, db, monkeypatch, client):
    _bridges(db)
    _board_user(db)
    _tg_updates(monkeypatch, [_tg_message("кто я")])

    telegram_bot.poll_and_link_accounts()

    message = db.query(BoardChatMessage).one()
    assert message.author_id is None and message.external_author == "Пётр Иванов"
    login(client, "board1", "pass1234")
    [shown] = client.get("/board-chat/messages").get_json()["messages"]
    assert shown["author_name"] == "Пётр Иванов (TG)" and shown["is_mine"] is False


def test_linked_non_board_member_shown_by_telegram_name(app, db, monkeypatch):
    _bridges(db)
    person = make_person(db, full_name="Просто Член", telegram_chat_id=5001)
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    _tg_updates(monkeypatch, [_tg_message("привет")])

    telegram_bot.poll_and_link_accounts()

    message = db.query(BoardChatMessage).one()
    assert message.author_id is None and message.external_author == "Пётр Иванов"


def test_telegram_duplicate_and_foreign_and_bot_messages_skipped(app, db, monkeypatch):
    _bridges(db)
    _tg_updates(monkeypatch, [
        _tg_message("раз", message_id=1),
        _tg_message("раз (повтор апдейта)", message_id=1),
        _tg_message("чужая группа", message_id=2, chat_id="-100555"),
        _tg_message("бот", message_id=3, **{"from": {"id": 1, "is_bot": True, "first_name": "Bot"}}),
        _tg_message("/help", message_id=4),
        {"message_id": 5, "chat": {"id": int(TG_GROUP)}, "from": {"id": 5001}, "new_chat_members": [{}]},
        {"message_id": 6, "chat": {"id": int(TG_GROUP)}, "from": {"id": 5001, "first_name": "Пётр"},
         "photo": [{}], "caption": "смотрите"},
    ])

    telegram_bot.poll_and_link_accounts()

    assert [m.body for m in db.query(BoardChatMessage).order_by(BoardChatMessage.id)] == [
        "раз", "[вложение в Telegram] смотрите"]


def test_unread_badge_counts_external_messages(app, db, monkeypatch, client):
    _bridges(db)
    _board_user(db)
    _tg_updates(monkeypatch, [_tg_message("непрочитанное")])
    telegram_bot.poll_and_link_accounts()

    login(client, "board1", "pass1234")
    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True) if resp.status_code == 200 else client.get("/cabinet/garages").get_data(as_text=True)
    assert 'id="boardChatBadge" class="chat-widget-badge ">1</span>' in body


# ---------------------------------------------------------------------------
# MAX -> сайт
# ---------------------------------------------------------------------------

def test_max_message_from_linked_board_member(app, db, monkeypatch):
    _bridges(db)
    user = _board_user(db, max_user_id=42)
    fake = FakeMax(monkeypatch)
    _max_updates(monkeypatch, fake, {"updates": [
        {"update_type": "message_created", "message": {
            "sender": {"user_id": 42, "name": "Иван"},
            "recipient": {"chat_id": int(MAX_GROUP), "chat_type": "chat"},
            "body": {"mid": "mid.1", "text": "из MAX"}}},
        {"update_type": "message_created", "message": {
            "sender": {"user_id": 9, "name": "Бот", "is_bot": True},
            "recipient": {"chat_id": int(MAX_GROUP), "chat_type": "chat"},
            "body": {"mid": "mid.2", "text": "board1: эхо"}}},
    ], "marker": 3})

    max_bot.poll_updates()

    message = db.query(BoardChatMessage).one()
    assert (message.author_id, message.source, message.body) == (user.id, "max", "из MAX")
    assert _outbox(db) == [("telegram", "group", TG_GROUP, "board1: из MAX")]


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def _chairman(db, client):
    person = make_person(db, full_name="Председатель")
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()
    login(client, "chair", "pass1234")


def test_telegram_settings_save_board_chat_id(db, client):
    _chairman(db, client)
    client.post("/telegram/settings", data={"bot_username": "coop_bot", "board_chat_id": TG_GROUP})
    assert db.query(TelegramSettings).one().board_chat_id == TG_GROUP

    client.post("/telegram/settings", data={"bot_username": "coop_bot", "board_chat_id": "@group"})
    assert db.query(TelegramSettings).one().board_chat_id == TG_GROUP  # неверный ID не сохранён


def test_max_board_button_keeps_news_chat(db, client):
    _chairman(db, client)
    db.add(MaxSettings(bot_token_encrypted=crypto.encrypt("t"), news_chat_id="-1"))
    db.commit()
    client.post("/max/settings", data={"board_chat_id": MAX_GROUP})
    settings = db.query(MaxSettings).one()
    assert (settings.news_chat_id, settings.board_chat_id) == ("-1", MAX_GROUP)


def test_max_board_invite_link_rejected_with_hint(db, client):
    _chairman(db, client)
    db.add(MaxSettings(bot_token_encrypted=crypto.encrypt("t"), news_chat_id="-1"))
    db.commit()
    resp = client.post("/max/settings", data={"news_chat_id": "-1", "board_chat_id": "https://max.ru/join/abc"},
                       follow_redirects=True)
    assert "не ссылкой-приглашением" in resp.get_data(as_text=True)
    assert db.query(MaxSettings).one().board_chat_id is None


def test_chatid_reply_failure_logged(app, db, monkeypatch, caplog):
    _bridges(db, max_=False)
    _tg_updates(monkeypatch, [_tg_message("/chatid", chat_id="-100123")])
    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **kw: FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"}))
    with caplog.at_level("INFO", logger="app.telegram_bot"):
        telegram_bot.poll_and_link_accounts()
    assert "команда /chatid" in caplog.text
    assert "не удалось ответить на /chatid" in caplog.text
