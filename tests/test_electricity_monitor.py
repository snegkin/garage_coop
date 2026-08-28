"""
Тесты подсистемы мониторинга электроэнергии по фазам (app/ewelink/,
app/electricity_monitor.py, scripts/poll_ewelink.py).

Реальные сетевые запросы к облаку eWeLink не делаются (домен не в allowlist
сети окружения, а главное — сама неофициальная логика входа не подтверждена
живым тестом, см. app/ewelink/client.py): роуты и поллер тестируются с
подменённым (monkeypatch) клиентом, как и SberbankClient в test_bank_sync.py.
Разбор ответа устройства (parse_phase_snapshot) проверяется отдельно, без сети.
"""
import datetime as dt
from decimal import Decimal

import pytest

from app import database
from app.bank_api import crypto
from app.models import RoleEnum, EWeLinkAccount, PowerPhaseDevice, PowerPhaseReading
from app.ewelink import EWeLinkTokens, EWeLinkApiError, EWeLinkAuthError, parse_phase_snapshot

from tests.conftest import make_user, login


def make_ewelink_account(db_session, **kwargs):
    account = EWeLinkAccount(**kwargs)
    db_session.add(account)
    db_session.flush()
    return account


def make_phase_device(db_session, label="Фаза A", ewelink_device_id="10023349b3", sort_order=0, **kwargs):
    device = PowerPhaseDevice(label=label, ewelink_device_id=ewelink_device_id, sort_order=sort_order, **kwargs)
    db_session.add(device)
    db_session.flush()
    return device


# ---------------------------------------------------------------------------
# Разбор ответа устройства (без сети)
# ---------------------------------------------------------------------------

def test_parse_phase_snapshot_reads_unsuffixed_keys():
    device = {"itemData": {"deviceid": "10023349b3", "online": True, "params": {
        "power": "612.3", "voltage": "231.5", "current": "2.65",
    }}}
    snap = parse_phase_snapshot(device)
    assert snap.power_w == Decimal("612.3")
    assert snap.voltage_v == Decimal("231.5")
    assert snap.current_a == Decimal("2.65")
    assert snap.is_online is True


def test_parse_phase_snapshot_reads_suffixed_keys():
    """Некоторые прошивки POWCT отдают параметры с суффиксом _00 (см.
    докстринг app/ewelink/client.py:POWER_KEYS) — не подтверждено живым
    тестом, но клиент должен разобрать оба варианта."""
    device = {"itemData": {"deviceid": "x", "online": True, "params": {
        "power_00": "100", "voltage_00": "220", "current_00": "0.45",
    }}}
    snap = parse_phase_snapshot(device)
    assert snap.power_w == Decimal("100")
    assert snap.voltage_v == Decimal("220")
    assert snap.current_a == Decimal("0.45")


def test_parse_phase_snapshot_missing_fields_are_none():
    device = {"itemData": {"deviceid": "x", "online": False, "params": {}}}
    snap = parse_phase_snapshot(device)
    assert snap.power_w is None
    assert snap.voltage_v is None
    assert snap.current_a is None
    assert snap.is_online is False


# ---------------------------------------------------------------------------
# Права доступа
# ---------------------------------------------------------------------------

def test_view_requires_board(app, db, client):
    make_user(db, "member1", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "member1", "pass12345")

    resp = client.get("/electricity/")
    assert resp.status_code == 302


def test_view_accessible_to_board(app, db, client):
    make_user(db, "board1", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board1", "pass12345")

    resp = client.get("/electricity/")
    assert resp.status_code == 200


def test_save_settings_requires_chairman(app, db, client):
    make_user(db, "board2", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board2", "pass12345")

    resp = client.post("/electricity/settings", data={
        "app_id": "aid", "app_secret": "secret", "email": "a@b.com", "password": "pw",
    })
    assert resp.status_code == 302
    db.expire_all()
    assert database.db_session.query(EWeLinkAccount).first() is None


# ---------------------------------------------------------------------------
# Сохранение настроек подключения (шифрование секретов)
# ---------------------------------------------------------------------------

def test_save_settings_stores_encrypted_secrets(app, db, client):
    make_user(db, "chair1", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair1", "pass12345")

    resp = client.post("/electricity/settings", data={
        "app_id": "my-app-id", "app_secret": "top-secret", "email": "coop@example.com", "password": "my-password",
    })
    assert resp.status_code == 302

    db.expire_all()
    account = database.db_session.query(EWeLinkAccount).first()
    assert account.app_id == "my-app-id"
    assert account.email == "coop@example.com"
    assert account.app_secret_encrypted != "top-secret"
    assert account.password_encrypted != "my-password"
    with app.app_context():
        assert crypto.decrypt(account.app_secret_encrypted) == "top-secret"
        assert crypto.decrypt(account.password_encrypted) == "my-password"


def test_save_settings_blank_secret_keeps_existing(app, db, client):
    account = make_ewelink_account(
        db, app_id="old-id", email="old@example.com",
        app_secret_encrypted=None, password_encrypted=None,
    )
    account.app_secret_encrypted = crypto.encrypt("original-secret")
    account.password_encrypted = crypto.encrypt("original-password")
    make_user(db, "chair2", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair2", "pass12345")

    resp = client.post("/electricity/settings", data={
        "app_id": "old-id", "app_secret": "", "email": "old@example.com", "password": "",
    })
    assert resp.status_code == 302

    db.expire_all()
    account = database.db_session.query(EWeLinkAccount).first()
    with app.app_context():
        assert crypto.decrypt(account.app_secret_encrypted) == "original-secret"
        assert crypto.decrypt(account.password_encrypted) == "original-password"


def test_save_settings_credential_change_resets_tokens(app, db, client):
    """Смена email/пароля/appId делает сохранённые токены бессмысленными —
    они получены со старыми учётными данными и должны сбрасываться."""
    account = make_ewelink_account(
        db, app_id="old-id", email="old@example.com",
        access_token_encrypted="enc-at", refresh_token_encrypted="enc-rt",
        token_obtained_at=dt.datetime.utcnow(),
    )
    account.app_secret_encrypted = crypto.encrypt("secret")
    account.password_encrypted = crypto.encrypt("password")
    make_user(db, "chair3", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair3", "pass12345")

    resp = client.post("/electricity/settings", data={
        "app_id": "new-id", "app_secret": "", "email": "old@example.com", "password": "",
    })
    assert resp.status_code == 302

    db.expire_all()
    account = database.db_session.query(EWeLinkAccount).first()
    assert account.app_id == "new-id"
    assert account.access_token_encrypted is None
    assert account.refresh_token_encrypted is None


# ---------------------------------------------------------------------------
# Привязка устройств к фазам
# ---------------------------------------------------------------------------

def test_save_devices_creates_three_phases(app, db, client):
    make_user(db, "chair4", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair4", "pass12345")

    resp = client.post("/electricity/devices", data={
        "label_a": "Фаза A", "device_id_a": "10023349b3",
        "label_b": "Фаза B", "device_id_b": "1002334597",
        "label_c": "Фаза C", "device_id_c": "1002333baf",
    })
    assert resp.status_code == 302

    db.expire_all()
    devices = (
        database.db_session.query(PowerPhaseDevice)
        .order_by(PowerPhaseDevice.sort_order)
        .all()
    )
    assert [d.ewelink_device_id for d in devices] == ["10023349b3", "1002334597", "1002333baf"]
    assert [d.label for d in devices] == ["Фаза A", "Фаза B", "Фаза C"]


def test_save_devices_skips_blank_device_id(app, db, client):
    """Третья фаза ещё не подключена — форма отправляется с пустым полем,
    и она просто не создаётся, вместо ошибки."""
    make_user(db, "chair5", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair5", "pass12345")

    resp = client.post("/electricity/devices", data={
        "label_a": "Фаза A", "device_id_a": "10023349b3",
        "label_b": "Фаза B", "device_id_b": "1002334597",
        "label_c": "Фаза C", "device_id_c": "",
    })
    assert resp.status_code == 302

    db.expire_all()
    devices = database.db_session.query(PowerPhaseDevice).all()
    assert len(devices) == 2


def test_save_devices_updates_existing_by_sort_order(app, db, client):
    make_phase_device(db, label="Фаза A", ewelink_device_id="old-device", sort_order=0)
    make_user(db, "chair6", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair6", "pass12345")

    resp = client.post("/electricity/devices", data={
        "label_a": "Фаза A (обновлено)", "device_id_a": "new-device",
        "label_b": "", "device_id_b": "",
        "label_c": "", "device_id_c": "",
    })
    assert resp.status_code == 302

    db.expire_all()
    devices = database.db_session.query(PowerPhaseDevice).all()
    assert len(devices) == 1
    assert devices[0].ewelink_device_id == "new-device"
    assert devices[0].label == "Фаза A (обновлено)"


# ---------------------------------------------------------------------------
# Отображение последних показаний
# ---------------------------------------------------------------------------

def test_view_shows_latest_reading_per_device(app, db, client):
    device = make_phase_device(db)
    db.add(PowerPhaseReading(
        device_id=device.id, ts=dt.datetime.utcnow() - dt.timedelta(minutes=5),
        power_w=Decimal("500"), voltage_v=Decimal("220"), current_a=Decimal("2.3"), is_online=True,
    ))
    db.add(PowerPhaseReading(
        device_id=device.id, ts=dt.datetime.utcnow(),
        power_w=Decimal("612.3"), voltage_v=Decimal("231.5"), current_a=Decimal("2.65"), is_online=True,
    ))
    make_user(db, "board3", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board3", "pass12345")

    resp = client.get("/electricity/")
    assert resp.status_code == 200
    # последний по времени снимок (612.3), не первый (500) — см. .desc() в electricity_monitor.view
    assert "612" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# test-connection: логин / refresh при протухшем токене
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, tokens=None, fail_list_devices_once=False, raise_on_login=None):
        self.tokens = tokens
        self._fail_once = fail_list_devices_once
        self._raise_on_login = raise_on_login
        self.rotated_refresh_token = None

    def login(self):
        if self._raise_on_login:
            raise self._raise_on_login
        self.tokens = EWeLinkTokens(access_token="at1", refresh_token="rt1", region="eu", obtained_at=0.0)
        return self.tokens

    def list_devices(self):
        if self._fail_once:
            self._fail_once = False
            raise EWeLinkAuthError("token expired")
        return []

    def refresh(self):
        self.tokens = EWeLinkTokens(access_token="at2", refresh_token="rt2", region="eu", obtained_at=0.0)
        return self.tokens


def test_test_connection_without_credentials_shows_warning(app, db, client):
    make_user(db, "chair7", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair7", "pass12345")

    resp = client.post("/electricity/test-connection", follow_redirects=True)
    assert resp.status_code == 200
    assert "заполните" in resp.get_data(as_text=True).lower()


def test_test_connection_logs_in_when_no_token(app, db, client, monkeypatch):
    make_ewelink_account(db, app_id="aid", email="a@b.com")
    account = database.db_session.query(EWeLinkAccount).first()
    account.app_secret_encrypted = crypto.encrypt("secret")
    account.password_encrypted = crypto.encrypt("password")
    make_user(db, "chair8", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair8", "pass12345")

    from app import electricity_monitor
    monkeypatch.setattr(electricity_monitor, "build_client", lambda acc: _FakeClient())

    resp = client.post("/electricity/test-connection", follow_redirects=True)
    assert resp.status_code == 200
    assert "работает" in resp.get_data(as_text=True).lower()

    db.expire_all()
    account = database.db_session.query(EWeLinkAccount).first()
    assert account.access_token_encrypted is not None
    with app.app_context():
        assert crypto.decrypt(account.access_token_encrypted) == "at1"


def test_test_connection_refreshes_on_expired_token(app, db, client, monkeypatch):
    make_ewelink_account(db, app_id="aid", email="a@b.com")
    account = database.db_session.query(EWeLinkAccount).first()
    account.app_secret_encrypted = crypto.encrypt("secret")
    account.password_encrypted = crypto.encrypt("password")
    account.access_token_encrypted = crypto.encrypt("old-at")
    account.refresh_token_encrypted = crypto.encrypt("old-rt")
    account.token_obtained_at = dt.datetime.utcnow()
    make_user(db, "chair9", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair9", "pass12345")

    existing_tokens = EWeLinkTokens(access_token="old-at", refresh_token="old-rt", region="eu", obtained_at=0.0)
    from app import electricity_monitor
    monkeypatch.setattr(
        electricity_monitor, "build_client",
        lambda acc: _FakeClient(tokens=existing_tokens, fail_list_devices_once=True),
    )

    resp = client.post("/electricity/test-connection", follow_redirects=True)
    assert resp.status_code == 200
    assert "работает" in resp.get_data(as_text=True).lower()

    db.expire_all()
    account = database.db_session.query(EWeLinkAccount).first()
    with app.app_context():
        assert crypto.decrypt(account.access_token_encrypted) == "at2"


def test_test_connection_shows_api_error(app, db, client, monkeypatch):
    make_ewelink_account(db, app_id="aid", email="a@b.com")
    account = database.db_session.query(EWeLinkAccount).first()
    account.app_secret_encrypted = crypto.encrypt("secret")
    account.password_encrypted = crypto.encrypt("password")
    make_user(db, "chair10", "pass12345", role=RoleEnum.CHAIRMAN)
    db.commit()
    login(client, "chair10", "pass12345")

    from app import electricity_monitor
    monkeypatch.setattr(
        electricity_monitor, "build_client",
        lambda acc: _FakeClient(raise_on_login=EWeLinkApiError("сеть недоступна")),
    )

    resp = client.post("/electricity/test-connection", follow_redirects=True)
    assert resp.status_code == 200
    assert "сеть недоступна" in resp.get_data(as_text=True)

    db.expire_all()
    account = database.db_session.query(EWeLinkAccount).first()
    assert account.last_error == "сеть недоступна"
