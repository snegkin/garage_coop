"""Один бот MAX на все кооперативы сервера (app/max_router.py)."""
import pytest

from app import database, max_bot, max_router
from app.bank_api import crypto
from app.models import MaxSettings, MaxUserRoute, Person

from tests.conftest import make_person
from tests.test_max_news import FakeMax, FakeResponse

SECRET = "route-secret-123"


def _settings(db):
    s = MaxSettings(bot_token_encrypted=crypto.encrypt("max-token"), bot_username="id77_bot")
    db.add(s)
    db.commit()
    return s


@pytest.fixture()
def main_coop(app):
    app.config.update(MAX_ROUTE_SECRET=SECRET, MAX_ROUTE_PEERS="a2=http://127.0.0.1:8001, a4=http://127.0.0.1:8002")
    return app


@pytest.fixture()
def peer_coop(app):
    app.config.update(MAX_ROUTE_SECRET=SECRET, MAX_COOP_CODE="a2", MAX_RECEIVE="forwarded")
    return app


def test_without_config_everything_is_local(app, db):
    with app.test_request_context():
        assert max_router.peers() == {}
        assert max_router.link_payload("tok") == "tok"
        updates = [{"update_type": "bot_started", "payload": "a2__tok"}]
        assert max_router.dispatch(updates) == updates


def test_peer_link_payload_has_coop_code(peer_coop, db):
    with peer_coop.test_request_context():
        assert max_router.link_payload("abc") == "a2__abc"
        assert max_router.strip_payload("a2__abc") == "abc"
        assert max_router.strip_payload("abc") == "abc"
        assert max_router.strip_payload("a4__abc") == "a4__abc"  # чужая метка — не наш токен
        s = _settings(db)
        assert max_bot.link_url(s, "abc") == "https://max.ru/id77_bot?start=a2__abc"


def test_peer_does_not_poll(peer_coop, db, monkeypatch):
    _settings(db)
    calls = []
    monkeypatch.setattr(max_bot.requests, "request", lambda *a, **k: calls.append(a))
    with peer_coop.app_context():
        assert max_bot.poll_updates() == (0, 0)
    assert calls == []


def test_main_dispatches_and_remembers_route(main_coop, db, monkeypatch):
    _settings(db)
    fake = FakeMax(monkeypatch)
    sent = []

    def post(url, json=None, headers=None, timeout=None, **kw):
        sent.append((url, json, headers))
        linked = [u["user"]["user_id"] for u in json["updates"] if u.get("update_type") == "bot_started"]
        return FakeResponse(200, {"linked": linked})

    batch = {"updates": [
        {"update_type": "bot_started", "payload": "a2__tok2", "user": {"user_id": 501}},
        {"update_type": "bot_started", "payload": "localtok", "user": {"user_id": 502}},
        {"update_type": "bot_added", "chat_id": -77, "is_channel": False},
        {"update_type": "message_created", "message": {"recipient": {"chat_id": 9, "chat_type": "dialog"},
                                                       "body": {"text": "привет"}, "sender": {"user_id": 503}}},
    ], "marker": 10}
    original = fake.request

    def request(method, url, params=None, **kw):
        if url.endswith("/updates"):
            return FakeResponse(200, batch)
        return original(method, url, params=params, **kw)

    monkeypatch.setattr(max_bot.requests, "request", request)
    monkeypatch.setattr(max_router.requests, "post", post)
    me = make_person(db, "Местный Житель")
    me.max_link_token = "localtok"
    db.commit()
    with main_coop.app_context():
        total, linked = max_bot.poll_updates()
    assert (total, linked) == (4, 1)
    by_url = {url: body["updates"] for url, body, _h in sent}
    a2 = by_url["http://127.0.0.1:8001/max/route"]
    a4 = by_url["http://127.0.0.1:8002/max/route"]
    assert [u["update_type"] for u in a2] == ["bot_started", "bot_added"]
    assert [u["update_type"] for u in a4] == ["bot_added"]  # группы — всем, привязка — только своему
    assert all(h[max_router.SECRET_HEADER] == SECRET for _u, _b, h in sent)
    assert db.query(Person).filter_by(max_user_id=502).count() == 1  # свой токен — привязан здесь
    route = db.query(MaxUserRoute).one()
    assert (route.max_user_id, route.coop_code) == (501, "a2")


def test_peer_route_endpoint(peer_coop, client, db, monkeypatch):
    _settings(db)
    FakeMax(monkeypatch)
    person = make_person(db, "Член Второго")
    person.max_link_token = "tok2"
    db.commit()
    body = {"updates": [{"update_type": "bot_started", "payload": "a2__tok2", "user": {"user_id": 501}}]}
    assert client.post("/max/route", json=body).status_code == 403
    assert client.post("/max/route", json=body, headers={max_router.SECRET_HEADER: "wrong"}).status_code == 403
    resp = client.post("/max/route", json=body, headers={max_router.SECRET_HEADER: SECRET})
    assert resp.status_code == 200 and resp.get_json()["linked"] == [501]
    db.refresh(person)
    assert person.max_user_id == 501 and person.max_link_token is None


def test_route_endpoint_closed_on_main(main_coop, client, db):
    resp = client.post("/max/route", json={"updates": []}, headers={max_router.SECRET_HEADER: SECRET})
    assert resp.status_code == 403


def test_link_without_bot_name_warns(app, client, db):
    from tests.conftest import login, make_user
    from app.models import RoleEnum
    db.add(MaxSettings(bot_token_encrypted=crypto.encrypt("max-token")))  # «Проверить подключение» не нажимали
    person = make_person(db, "Без Имени Бота")
    make_user(db, "nobotname", "pw", RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "nobotname", "pw")
    html = client.post("/cabinet/profile/max/link", follow_redirects=True).get_data(as_text=True)
    assert "сайт не знает имя бота" in html


def test_messenger_settings_menu_only_for_chairman(client, db):
    from tests.conftest import login, make_user
    from app.models import RoleEnum
    make_user(db, "boardonly", "pw", RoleEnum.BOARD)
    make_user(db, "chaironly", "pw", RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "boardonly", "pw")
    assert "СМС и мессенджеры" not in client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    client.get("/auth/logout")
    login(client, "chaironly", "pw")
    assert "СМС и мессенджеры" in client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
