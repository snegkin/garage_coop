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


def test_chatid_command_case_insensitive():
    assert telegram_bot._is_chatid_command("/chatId")
    assert telegram_bot._is_chatid_command("/CHATID@coop_bot")
    assert not telegram_bot._is_chatid_command("/chatids")


# ---------------------------------------------------------------------------
# Удаление и закреп
# ---------------------------------------------------------------------------

def _post(client, body="сообщение"):
    return client.post("/board-chat/messages", data={"body": body}).get_json()["message"]["id"]


def _actions(db):
    return sorted((i.channel, i.kind) for i in db.query(MessengerOutbox)
                  if i.kind.startswith("group_"))


def test_delete_own_message_hides_it_and_relays(db, client):
    _bridges(db)
    _board_user(db)
    login(client, "board1", "pass1234")
    message_id = _post(client)

    assert client.post(f"/board-chat/messages/{message_id}/delete").status_code == 200

    data = client.get("/board-chat/messages").get_json()
    assert data["messages"] == [] and data["deleted_ids"] == [message_id]
    assert db.get(BoardChatMessage, message_id).deleted_at is not None
    assert _actions(db) == [("max", "group_delete"), ("telegram", "group_delete")]


def test_only_author_or_chairman_can_delete(db, client):
    _board_user(db, "board1")
    _board_user(db, "board2")
    chair = make_person(db, full_name="Председатель")
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=chair)
    db.commit()
    login(client, "board1", "pass1234")
    message_id = _post(client)
    assert client.get("/board-chat/messages").get_json()["messages"][0]["can_delete"] is True
    client.get("/auth/logout")

    login(client, "board2", "pass1234")
    assert client.get("/board-chat/messages").get_json()["messages"][0]["can_delete"] is False
    assert client.post(f"/board-chat/messages/{message_id}/delete").status_code == 403
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    assert client.post(f"/board-chat/messages/{message_id}/delete").status_code == 200


def test_pin_keeps_single_pinned_and_relays(db, client):
    _bridges(db, max_=False)
    _board_user(db)
    login(client, "board1", "pass1234")
    first, second = _post(client, "раз"), _post(client, "два")

    client.post(f"/board-chat/messages/{first}/pin")
    assert client.get("/board-chat/messages").get_json()["pinned"]["id"] == first
    client.post(f"/board-chat/messages/{second}/pin")
    assert client.get("/board-chat/messages").get_json()["pinned"]["id"] == second
    assert db.get(BoardChatMessage, first).pinned_at is None
    kinds = [(i.kind, i.board_message_id) for i in db.query(MessengerOutbox).order_by(MessengerOutbox.id)
             if i.kind.startswith("group_")]
    assert kinds == [("group_pin", first), ("group_unpin", first), ("group_pin", second)]

    client.post(f"/board-chat/messages/{second}/unpin")
    assert client.get("/board-chat/messages").get_json()["pinned"] is None


def test_actions_use_ids_of_sent_copies(app, db, client, monkeypatch):
    """Копия ушла — её id запомнен; удаление/закреп бьют по нему в обоих мессенджерах."""
    _bridges(db)
    _board_user(db)
    login(client, "board1", "pass1234")
    message_id = _post(client)

    fake = FakeMax(monkeypatch)
    tg_calls = []

    def tg_post(url, json=None, **kw):
        tg_calls.append((url.rsplit("/", 1)[-1], json))
        return FakeResponse(200, {"ok": True, "result": {"message_id": 777}})

    monkeypatch.setattr(telegram_bot.requests, "post", tg_post)
    messenger_outbox.process(app.config["UPLOAD_FOLDER"])
    copies = {i.channel: i.external_id for i in db.query(MessengerOutbox).filter_by(kind="group")}
    assert copies == {"telegram": "777", "max": "m1"}

    client.post(f"/board-chat/messages/{message_id}/pin")
    client.post(f"/board-chat/messages/{message_id}/delete")
    assert messenger_outbox.process(app.config["UPLOAD_FOLDER"]) == (4, 0)

    assert [c for c in tg_calls if c[0] != "sendMessage"] == [
        ("pinChatMessage", {"chat_id": TG_GROUP, "message_id": 777, "disable_notification": True}),
        ("deleteMessage", {"chat_id": TG_GROUP, "message_id": 777}),
    ]
    max_calls = [(m, p, prm, js) for m, p, prm, js, _h in fake.calls if p != "/messages" or m == "DELETE"]
    assert max_calls == [
        ("PUT", f"/chats/{MAX_GROUP}/pin", None, {"message_id": "m1", "notify": False}),
        ("DELETE", "/messages", {"message_id": "m1"}, None),
    ]


def test_delete_before_copy_sent_cancels_copy(app, db, client, monkeypatch):
    _bridges(db, max_=False)
    _board_user(db)
    login(client, "board1", "pass1234")
    message_id = _post(client)
    client.post(f"/board-chat/messages/{message_id}/delete")
    calls = []
    monkeypatch.setattr(telegram_bot.requests, "post", lambda url, json=None, **kw: calls.append(url) or FakeResponse(200))

    messenger_outbox.process(app.config["UPLOAD_FOLDER"])

    copy = db.query(MessengerOutbox).filter_by(kind="group").one()
    assert copy.status.value == "cancelled"
    assert calls == []  # ни отправки, ни удаления


def test_delete_message_from_telegram_deletes_original(app, db, client, monkeypatch):
    _bridges(db, max_=False)
    _board_user(db)
    chair = make_person(db, full_name="Председатель")
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=chair)
    db.commit()
    _tg_updates(monkeypatch, [_tg_message("из телеги", message_id=55)])
    telegram_bot.poll_and_link_accounts()
    message = db.query(BoardChatMessage).one()

    login(client, "chair", "pass1234")
    assert client.post(f"/board-chat/messages/{message.id}/delete").status_code == 200
    calls = []
    monkeypatch.setattr(telegram_bot.requests, "post", lambda url, json=None, **kw: calls.append(json) or FakeResponse(200))
    messenger_outbox.process(app.config["UPLOAD_FOLDER"])
    assert calls == [{"chat_id": TG_GROUP, "message_id": 55}]


def test_telegram_sticker_shown_as_its_emoji(app, db, monkeypatch):
    _bridges(db, max_=False)
    _tg_updates(monkeypatch, [
        {"message_id": 7, "chat": {"id": int(TG_GROUP)}, "from": {"id": 5001, "first_name": "Пётр"},
         "sticker": {"emoji": "😂", "file_id": "x"}},
    ])
    telegram_bot.poll_and_link_accounts()
    assert db.query(BoardChatMessage).one().body == "[стикер 😂]"


def test_emoji_passes_through_bridge_as_text(db, client):
    _bridges(db, max_=False)
    _board_user(db)
    login(client, "board1", "pass1234")
    client.post("/board-chat/messages", data={"body": "готово 👍😀"})
    assert db.query(MessengerOutbox).one().text == "board1: готово 👍😀"


def test_widget_has_emoji_picker(db, client):
    _board_user(db)
    login(client, "board1", "pass1234")
    body = client.get("/dashboard").get_data(as_text=True)
    if "chat-widget-emoji-panel" not in body:
        body = client.get("/cabinet/garages").get_data(as_text=True)
    assert "chat-widget-emoji-panel" in body and "bi-emoji-smile" in body



# ---------------------------------------------------------------------------
# Ответы и правка через мост
# ---------------------------------------------------------------------------

def _sent_copies(app, monkeypatch):
    """Отправить очередь: Telegram отвечает message_id=777, MAX — mid m1."""
    fake = FakeMax(monkeypatch)
    tg_calls = []

    def tg_post(url, json=None, **kw):
        tg_calls.append((url.rsplit("/", 1)[-1], json))
        return FakeResponse(200, {"ok": True, "result": {"message_id": 777}})

    monkeypatch.setattr(telegram_bot.requests, "post", tg_post)
    messenger_outbox.process(app.config["UPLOAD_FOLDER"])
    return fake, tg_calls


def test_site_reply_sent_as_reply_to_copy(app, db, client, monkeypatch):
    _bridges(db)
    _board_user(db)
    login(client, "board1", "pass1234")
    first = _post(client, "вопрос")
    _sent_copies(app, monkeypatch)

    client.post("/board-chat/messages", data={"body": "ответ", "reply_to_id": first})
    fake, tg_calls = _sent_copies(app, monkeypatch)

    assert tg_calls == [("sendMessage", {
        "chat_id": TG_GROUP, "text": "board1: ответ",
        "reply_parameters": {"message_id": 777, "allow_sending_without_reply": True}})]
    assert fake.messages[0]["body"]["link"] == {"type": "reply", "mid": "m1"}


def test_reply_to_message_without_copy_quotes_it(app, db, client, monkeypatch):
    """Сообщение, на которое отвечают, в группу не ушло — цитата в тексте."""
    _board_user(db)
    login(client, "board1", "pass1234")
    first = _post(client, "старое")  # до моста
    _bridges(db, max_=False)
    client.post("/board-chat/messages", data={"body": "ответ", "reply_to_id": first})
    _fake, tg_calls = _sent_copies(app, monkeypatch)
    assert tg_calls == [("sendMessage", {"chat_id": TG_GROUP, "text": "↩ board1: «старое»\nboard1: ответ"})]


def test_site_edit_edits_copies(app, db, client, monkeypatch):
    _bridges(db)
    _board_user(db)
    login(client, "board1", "pass1234")
    message_id = _post(client, "было")
    _sent_copies(app, monkeypatch)

    client.post(f"/board-chat/messages/{message_id}/edit", data={"body": "стало"})
    fake, tg_calls = _sent_copies(app, monkeypatch)

    assert tg_calls == [("editMessageText", {"chat_id": TG_GROUP, "message_id": 777, "text": "board1: стало"})]
    assert [(m, p, prm, js) for m, p, prm, js, _h in fake.calls] == [
        ("PUT", "/messages", {"message_id": "m1"}, {"text": "board1: стало"})]


def test_edit_before_copy_sent_updates_pending_copy(db, client):
    _bridges(db, max_=False)
    _board_user(db)
    login(client, "board1", "pass1234")
    message_id = _post(client, "было")
    client.post(f"/board-chat/messages/{message_id}/edit", data={"body": "стало"})
    assert _outbox(db) == [("telegram", "group", TG_GROUP, "board1: стало")]


def test_telegram_reply_and_edit_reach_site(app, db, monkeypatch):
    _bridges(db)
    _board_user(db, telegram_chat_id=5001)
    _tg_updates(monkeypatch, [_tg_message("вопрос", message_id=10)])
    telegram_bot.poll_and_link_accounts()
    question = db.query(BoardChatMessage).one()

    # Ответ на сообщение из группы и правка вопроса.
    reply = _tg_message("ответ", message_id=11, reply_to_message={"message_id": 10})
    edited = _tg_message("вопрос (уточнил)", message_id=10)
    result = [{"update_id": 5, "message": reply}, {"update_id": 6, "edited_message": edited}]
    monkeypatch.setattr(telegram_bot.requests, "get",
                        lambda *a, **kw: FakeResponse(200, {"ok": True, "result": result}))
    telegram_bot.poll_and_link_accounts()

    answer = db.query(BoardChatMessage).filter_by(body="ответ").one()
    assert answer.reply_to_id == question.id
    db.refresh(question)
    assert question.body == "вопрос (уточнил)" and question.edited_at is not None
    # Копия в MAX ещё ждала отправки — уйдёт уже с новым текстом; обратно
    # в Telegram ничего.
    assert _outbox(db) == [("max", "group", MAX_GROUP, "board1: вопрос (уточнил)"),
                           ("max", "group", MAX_GROUP, "board1: ответ")]


def test_telegram_reply_to_bot_copy_links_site_message(app, db, client, monkeypatch):
    _bridges(db, max_=False)
    _board_user(db, telegram_chat_id=5001)
    login(client, "board1", "pass1234")
    first = _post(client, "с сайта")
    _sent_copies(app, monkeypatch)  # копия в Telegram — message_id 777

    _tg_updates(monkeypatch, [_tg_message("ответ из TG", message_id=12, reply_to_message={"message_id": 777})])
    telegram_bot.poll_and_link_accounts()
    assert db.query(BoardChatMessage).filter_by(body="ответ из TG").one().reply_to_id == first


def test_max_reply_and_edit_reach_site(app, db, monkeypatch):
    _bridges(db)
    _board_user(db, max_user_id=42)
    fake = FakeMax(monkeypatch)

    def msg(mid, text, **extra):
        return {"sender": {"user_id": 42, "name": "Иван"},
                "recipient": {"chat_id": int(MAX_GROUP), "chat_type": "chat"},
                "body": {"mid": mid, "text": text}, **extra}

    _max_updates(monkeypatch, fake, {"updates": [
        {"update_type": "message_created", "message": msg("mid.1", "вопрос")},
        {"update_type": "message_created", "message": msg("mid.2", "ответ",
                                                          link={"type": "reply", "message": {"mid": "mid.1"}})},
        {"update_type": "message_edited", "message": msg("mid.1", "вопрос!")},
    ], "marker": 4})
    max_bot.poll_updates()

    question = db.query(BoardChatMessage).filter_by(external_message_id="mid.1").one()
    answer = db.query(BoardChatMessage).filter_by(external_message_id="mid.2").one()
    assert answer.reply_to_id == question.id
    assert question.body == "вопрос!"


# ---------------------------------------------------------------------------
# Реакции из Telegram
# ---------------------------------------------------------------------------

def _tg_raw_updates(monkeypatch, updates):
    result = [{"update_id": 100 + n, **u} for n, u in enumerate(updates)]
    calls = []

    def fake_get(url, params=None, timeout=None, **kw):
        calls.append(params)
        return FakeResponse(200, {"ok": True, "result": result})

    monkeypatch.setattr(telegram_bot.requests, "get", fake_get)
    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **kw: FakeResponse(200))
    return calls


def _reaction(message_id, emojis, user=None, chat_id=TG_GROUP):
    return {"message_reaction": {
        "chat": {"id": int(chat_id), "type": "supergroup"}, "message_id": message_id,
        "user": user or {"id": 5001, "first_name": "Пётр", "last_name": "Иванов"},
        "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": e} for e in emojis]}}


def test_telegram_reactions_requested_and_reach_site(app, db, client, monkeypatch):
    """Реакция на копию сообщения с сайта: от привязанного — его логином,
    от непривязанного — именем из Telegram; ❤ приводится к ❤️ сайта."""
    _bridges(db, max_=False)
    _board_user(db, telegram_chat_id=5001)
    login(client, "board1", "pass1234")
    message_id = _post(client, "с сайта")
    _sent_copies(app, monkeypatch)  # копия в Telegram — 777

    calls = _tg_raw_updates(monkeypatch, [
        _reaction(777, ["❤"]),
        _reaction(777, ["❤", "🤝"], user={"id": 9, "first_name": "Гость"}),
    ])
    telegram_bot.poll_and_link_accounts()
    assert "message_reaction" in calls[0]["allowed_updates"]

    [shown] = client.get("/board-chat/messages").get_json()["messages"]
    assert shown["reactions"] == [
        {"emoji": "❤️", "count": 2, "mine": True, "users": ["board1", "Гость (TG)"]},
        {"emoji": "🤝", "count": 1, "mine": False, "users": ["Гость (TG)"]},
    ]
    # К реакции из Telegram можно присоединиться с сайта, хоть её нет в наборе.
    assert client.post(f"/board-chat/messages/{message_id}/react", data={"emoji": "🤝"}).status_code == 200


def test_telegram_reaction_replaced_by_new_set_keeps_site_reaction(app, db, client, monkeypatch):
    _bridges(db, max_=False)
    _board_user(db, telegram_chat_id=5001)
    _tg_updates(monkeypatch, [_tg_message("вопрос", message_id=10)])
    telegram_bot.poll_and_link_accounts()
    message = db.query(BoardChatMessage).one()
    login(client, "board1", "pass1234")
    client.post(f"/board-chat/messages/{message.id}/react", data={"emoji": "🔥"})

    _tg_raw_updates(monkeypatch, [_reaction(10, ["👍"]), _reaction(10, [])])  # поставил и снял
    telegram_bot.poll_and_link_accounts()

    [shown] = client.get("/board-chat/messages").get_json()["messages"]
    assert [r["emoji"] for r in shown["reactions"]] == ["🔥"]


def test_reaction_in_other_chat_ignored(app, db, monkeypatch):
    _bridges(db, max_=False)
    _board_user(db)
    _tg_updates(monkeypatch, [_tg_message("вопрос", message_id=10)])
    telegram_bot.poll_and_link_accounts()
    _tg_raw_updates(monkeypatch, [_reaction(10, ["👍"], chat_id="-100555")])
    telegram_bot.poll_and_link_accounts()
    from app.models import BoardChatReaction
    assert db.query(BoardChatReaction).count() == 0
