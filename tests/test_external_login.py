"""
Вход через Яндекс ID и VK ID (app/external_login.py): кнопки на форме,
сопоставление по телефону, привязка/отвязка в профиле, настройки.
Обмен кода с провайдером подменяется — сеть в тестах не трогаем.
"""
from urllib.parse import parse_qs, urlsplit

import pytest

from app import external_login
from app.bank_api import crypto
from app.external_login import Profile
from app.models import ExternalIdentity, ExternalLoginSettings, Phone, RoleEnum, User

from tests.conftest import login, make_person, make_user


@pytest.fixture()
def configured(db):
    db.add(ExternalLoginSettings(
        yandex_client_id="ya-client", yandex_client_secret_encrypted=crypto.encrypt("ya-secret"),
        vk_client_id="51234567",
    ))
    db.commit()


@pytest.fixture()
def fake_profile(monkeypatch):
    holder = {"profile": Profile(subject="1001", phone_digits="9001234567", display_name="ivanov")}
    monkeypatch.setattr(external_login, "yandex_profile", lambda settings, code: holder["profile"])
    calls = []

    def vk(settings, code, device_id, state, verifier, redirect_uri):
        calls.append({"code": code, "device_id": device_id, "verifier": verifier})
        return holder["profile"]

    monkeypatch.setattr(external_login, "vk_profile", vk)
    holder["vk_calls"] = calls
    return holder


def _person_with_phone(db, number="+7 (900) 123-45-67", full_name="Иванов Иван Иванович"):
    person = make_person(db, full_name)
    db.add(Phone(person_id=person.id, number=number, label="мобильный"))
    db.flush()
    return person


def _start(client, provider="yandex", data=None, next_url=None):
    url = f"/auth/external/{provider}/start" + (f"?next={next_url}" if next_url else "")
    resp = client.post(url, data=data or {})
    assert resp.status_code == 302
    return parse_qs(urlsplit(resp.headers["Location"]).query)


def _callback(client, provider="yandex", follow_redirects=False, **params):
    return client.get(f"/auth/external/{provider}/callback", query_string=params, follow_redirects=follow_redirects)


def _logged_in_user_id(client):
    with client.session_transaction() as sess:
        return sess.get("user_id")


def test_buttons_hidden_until_configured(client, db):
    assert "/auth/external/" not in client.get("/auth/login").get_data(as_text=True)
    assert client.post("/auth/external/yandex/start").status_code == 404


def test_buttons_shown_when_configured(client, configured):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "/auth/external/yandex/start" in html
    assert "/auth/external/vk/start" in html


def test_yandex_authorize_redirect(client, configured):
    params = _start(client)
    assert params["client_id"] == ["ya-client"]
    assert params["redirect_uri"][0].endswith("/auth/external/yandex/callback")
    assert "login:default_phone" in params["scope"][0]
    assert params["state"][0]


def test_vk_authorize_uses_pkce(client, configured):
    params = _start(client, "vk")
    assert params["client_id"] == ["51234567"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    assert params["scope"] == ["phone"]


def test_state_mismatch_rejected(client, db, configured, fake_profile):
    _person_with_phone(db)
    db.commit()
    _start(client)
    resp = _callback(client, code="c", state="wrong")
    assert resp.status_code == 302
    assert _logged_in_user_id(client) is None


def test_first_login_by_phone_creates_account(client, db, configured, fake_profile):
    person = _person_with_phone(db)
    person.is_board_member = True
    db.commit()
    state = _start(client, data={"remember_me": "1"})["state"][0]
    resp = _callback(client, code="c", state=state)
    assert resp.status_code == 302
    user = db.query(User).filter_by(person_id=person.id).one()
    assert user.role == RoleEnum.BOARD
    assert _logged_in_user_id(client) == user.id
    identity = db.query(ExternalIdentity).one()
    assert (identity.provider, identity.subject, identity.user_id) == ("yandex", "1001", user.id)
    with client.session_transaction() as sess:
        assert sess.permanent is True


def test_existing_account_linked_by_phone_and_next_kept(client, db, configured, fake_profile):
    person = _person_with_phone(db)
    user = make_user(db, "ivanov", "pw", person=person)
    db.commit()
    state = _start(client, next_url="/cabinet/garages")["state"][0]
    resp = _callback(client, code="c", state=state)
    assert resp.headers["Location"].endswith("/cabinet/garages")
    assert _logged_in_user_id(client) == user.id
    assert db.query(ExternalIdentity).one().user_id == user.id


def test_linked_identity_logs_in_even_if_phone_changed(client, db, configured, fake_profile):
    user = make_user(db, "petrov", "pw")
    db.add(ExternalIdentity(user_id=user.id, provider="yandex", subject="1001"))
    db.commit()
    fake_profile["profile"] = Profile(subject="1001", phone_digits=None, display_name="petrov")
    state = _start(client)["state"][0]
    _callback(client, code="c", state=state)
    assert _logged_in_user_id(client) == user.id


def test_unknown_phone_refused(client, db, configured, fake_profile):
    _person_with_phone(db, number="+7 911 000-00-00")
    db.commit()
    state = _start(client)["state"][0]
    _callback(client, code="c", state=state)
    assert _logged_in_user_id(client) is None
    assert db.query(User).count() == 0
    assert db.query(ExternalIdentity).count() == 0


def test_ambiguous_phone_refused(client, db, configured, fake_profile):
    _person_with_phone(db, full_name="Первый")
    _person_with_phone(db, full_name="Второй")
    db.commit()
    state = _start(client)["state"][0]
    resp = _callback(client, code="c", state=state, follow_redirects=True)
    assert _logged_in_user_id(client) is None
    assert "в нескольких карточках" in resp.get_data(as_text=True)


def test_cancel_shows_calm_message_without_audit(client, db, configured):
    from app.models import AuditLog
    _start(client)
    resp = _callback(client, error="access_denied", follow_redirects=True)
    assert "Вход через Яндекс ID отменён" in resp.get_data(as_text=True)
    assert db.query(AuditLog).filter_by(action="auth.login_failed").count() == 0


def test_provider_failure_hides_details_but_audits_them(client, db, configured, monkeypatch):
    from app.models import AuditLog

    def broken(settings, code):
        raise external_login.ExternalLoginError("Яндекс ID, получение токена: invalid_grant")

    monkeypatch.setattr(external_login, "yandex_profile", broken)
    state = _start(client)["state"][0]
    html = _callback(client, code="c", state=state, follow_redirects=True).get_data(as_text=True)
    assert "Не удалось связаться с Яндекс ID" in html
    assert "invalid_grant" not in html
    entry = db.query(AuditLog).filter_by(action="auth.login_failed").one()
    assert "invalid_grant" in entry.summary


def test_inactive_account_refused(client, db, configured, fake_profile):
    person = _person_with_phone(db)
    user = make_user(db, "ivanov", "pw", person=person)
    user.is_active = False
    db.commit()
    state = _start(client)["state"][0]
    _callback(client, code="c", state=state)
    assert _logged_in_user_id(client) is None


def test_other_identity_already_linked_refused(client, db, configured, fake_profile):
    person = _person_with_phone(db)
    user = make_user(db, "ivanov", "pw", person=person)
    db.add(ExternalIdentity(user_id=user.id, provider="yandex", subject="999"))
    db.commit()
    state = _start(client)["state"][0]
    _callback(client, code="c", state=state)
    assert _logged_in_user_id(client) is None
    assert db.query(ExternalIdentity).count() == 1


def test_vk_callback_passes_device_id_and_verifier(client, db, configured, fake_profile):
    person = _person_with_phone(db)
    db.commit()
    state = _start(client, "vk")["state"][0]
    _callback(client, "vk", code="vk-code", state=state, device_id="dev-1", type="code_v2")
    call = fake_profile["vk_calls"][0]
    assert call["code"] == "vk-code" and call["device_id"] == "dev-1" and call["verifier"]
    assert db.query(User).filter_by(person_id=person.id).one()


def test_link_and_unlink_from_profile(client, db, configured, fake_profile):
    user = make_user(db, "sidorov", "pw")
    db.commit()
    login(client, "sidorov", "pw")
    fake_profile["profile"] = Profile(subject="vk-77", phone_digits=None, display_name="Сидор")
    state = _start(client, "vk")["state"][0]
    resp = _callback(client, "vk", code="c", state=state, device_id="d")
    assert resp.headers["Location"].endswith("/cabinet/profile")
    identity = db.query(ExternalIdentity).one()
    assert (identity.user_id, identity.provider, identity.display_name) == (user.id, "vk", "Сидор")

    client.post("/auth/external/vk/unlink")
    assert db.query(ExternalIdentity).count() == 0


def test_link_refused_when_identity_belongs_to_other_user(client, db, configured, fake_profile):
    other = make_user(db, "other", "pw")
    make_user(db, "me", "pw")
    db.add(ExternalIdentity(user_id=other.id, provider="yandex", subject="1001"))
    db.commit()
    login(client, "me", "pw")
    state = _start(client)["state"][0]
    _callback(client, code="c", state=state)
    assert db.query(ExternalIdentity).one().user_id == other.id


def test_settings_page_saves(client, db):
    make_user(db, "chair", "pw", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair", "pw")
    assert "/auth/external/yandex/callback" in client.get("/auth/external/settings").get_data(as_text=True)
    client.post("/auth/external/settings", data={
        "yandex_client_id": "abc", "yandex_client_secret": "s3cret", "vk_client_id": "123",
    })
    settings = db.query(ExternalLoginSettings).one()
    db.refresh(settings)
    assert crypto.decrypt(settings.yandex_client_secret_encrypted) == "s3cret"
    assert external_login.enabled_providers(settings) == ["yandex", "vk"]

    # пустой секрет не затирает сохранённый
    client.post("/auth/external/settings", data={"yandex_client_id": "abc", "yandex_client_secret": "", "vk_client_id": ""})
    db.refresh(settings)
    assert crypto.decrypt(settings.yandex_client_secret_encrypted) == "s3cret"
    assert external_login.enabled_providers(settings) == ["yandex"]


def test_settings_page_chairman_only(client, db):
    make_user(db, "member", "pw")
    db.commit()
    login(client, "member", "pw")
    client.post("/auth/external/settings", data={"vk_client_id": "123"})
    assert db.query(ExternalLoginSettings).count() == 0


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


def test_yandex_profile_parses_response(monkeypatch, db):
    settings = ExternalLoginSettings(yandex_client_id="id", yandex_client_secret_encrypted=crypto.encrypt("sec"))
    posted = {}

    def post(url, data, timeout):
        posted.update(data)
        return _Resp({"access_token": "tok"})

    def get(url, params, headers, timeout):
        assert headers["Authorization"] == "OAuth tok"
        return _Resp({"id": "42", "login": "ivan", "default_phone": {"id": 1, "number": "+79001234567"}})

    monkeypatch.setattr(external_login.requests, "post", post)
    monkeypatch.setattr(external_login.requests, "get", get)
    profile = external_login.yandex_profile(settings, "code")
    assert posted["client_secret"] == "sec"
    assert profile == Profile(subject="42", phone_digits="9001234567", display_name="ivan")


def test_vk_profile_parses_response(monkeypatch):
    settings = ExternalLoginSettings(vk_client_id="5")
    responses = iter([
        _Resp({"access_token": "tok", "user_id": 7}),
        _Resp({"user": {"user_id": 7, "first_name": "Иван", "last_name": "Петров", "phone": "79001234567"}}),
    ])
    monkeypatch.setattr(external_login.requests, "post", lambda url, data, timeout: next(responses))
    profile = external_login.vk_profile(settings, "c", "d", "s", "v", "https://x/cb")
    assert profile == Profile(subject="7", phone_digits="9001234567", display_name="Иван Петров")


def test_provider_error_reported(monkeypatch):
    settings = ExternalLoginSettings(vk_client_id="5")
    monkeypatch.setattr(
        external_login.requests, "post",
        lambda url, data, timeout: _Resp({"error": "invalid_grant", "error_description": "code expired"}, 400),
    )
    with pytest.raises(external_login.ExternalLoginError, match="code expired"):
        external_login.vk_profile(settings, "c", "d", "s", "v", "https://x/cb")


def test_profile_shows_link_buttons(client, db, configured):
    person = make_person(db, "Кузнецов Кузьма")
    user = make_user(db, "kuz", "pw", person=person)
    db.add(ExternalIdentity(user_id=user.id, provider="vk", subject="5", display_name="Кузьма К."))
    db.commit()
    login(client, "kuz", "pw")
    html = client.get("/cabinet/profile").get_data(as_text=True)
    assert "/auth/external/yandex/start" in html
    assert "/auth/external/vk/unlink" in html and "Кузьма К." in html
