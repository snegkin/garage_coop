"""
Смайлы в чатах картинками (app/chat_emoji.py): карта спрайта и подключение
к виджету чата правления.
"""
from app.chat_emoji import sprite_map
from app.models import RoleEnum
from tests.conftest import make_person, make_user, login


def test_sprite_map_covers_qualified_unqualified_and_skin_tones():
    data = sprite_map()
    assert data["cols"] == 62
    emoji = data["map"]
    assert "😀" in emoji
    assert emoji["❤️"] == emoji["❤"]        # с FE0F и без — одна клетка
    assert "👍🏽" in emoji and emoji["👍🏽"] != emoji["👍"]
    assert "👨‍💻" in emoji
    assert "🇷🇺" in emoji
    # Все реакции чата правления есть на картинках.
    from app.board_chat import REACTIONS
    assert all(r in emoji for r in REACTIONS)


def test_sprite_map_endpoint_is_compact_and_cached(client):
    resp = client.get("/chat/emoji-sprites.json?v=16.0.0")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert "max-age=31536000" in resp.headers["Cache-Control"]
    assert "😀".encode() in resp.data          # без \u-экранирования
    assert resp.get_json()["map"]["😀"]


def test_widget_gets_emoji_config(db, client):
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=make_person(db, full_name="Board One"))
    db.commit()
    login(client, "board1", "pass1234")
    body = client.get("/").get_data(as_text=True)
    assert "var chatEmoji" in body
    assert "/chat/emoji-sprites.json?v=" in body
    assert "emoji-datasource-apple-" in body and "emoji-datasource-twitter-" in body
    assert "emojiStyleLabel" in body
