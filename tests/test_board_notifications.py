"""
Уведомления правлению о событиях журнала аудита (app/notifications.py:
BOARD_EVENT_GROUPS, queue_board_event) и новые подписки членов —
голосование, собрание, решение по своей заявке, документ/вики.

Отправка — только после commit (очередь сессии, см.
notifications.after_commit), автору действия не приходит. Реально
отправляется email — SMTP подменён (см. tests/test_notifications.py).
"""
import datetime as dt
import json
import os
import re
from decimal import Decimal

from app import audit, bank_sync, database, notifications
from app.bank_api.base import StatementLine
from app.models import (
    RoleEnum, BankApiProvider, Vote, VoteQuestion, VoteStatus, PersonDataRevision, PersonDataRevisionStatus,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login
from tests.test_bank_sync import _StubClient, make_bank_account, make_credential
from tests.test_notifications import _mock_smtp, _make_mailbox_settings


def _board_user(db, username, role=RoleEnum.BOARD, email=None, groups=None):
    person = make_person(db, full_name=f"Член правления {username}", email=email or f"{username}@example.com")
    user = make_user(db, username, "pass12345", role=role, person=person)
    if groups is not None:
        user.notify_board_events = json.dumps(groups)
    return user


def _member(db, username="member1", email="member1@example.com", owner=True):
    person = make_person(db, full_name=f"Член кооператива {username}", email=email)
    if owner:
        make_ownership(db, make_garage(db, number=f"g-{username}"), person)
    user = make_user(db, username, "pass12345", role=RoleEnum.MEMBER, person=person)
    return user, person


def _recipients(fake_smtp) -> list[str]:
    return [msg["To"] for msg in fake_smtp.sent]


# ---------------------------------------------------------------------------
# Справочник групп
# ---------------------------------------------------------------------------

def test_every_audit_action_has_board_group_or_is_ignored():
    """Каждый код audit.record("…") в коде попадает в группу уведомлений
    правления либо явно проигнорирован — новое действие не потеряется молча."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pattern = re.compile(r'audit\.record\(\s*f?"([a-z_]+\.[a-z_]+)"')
    actions = set()
    for folder in ("app", "scripts"):
        for name in os.listdir(os.path.join(root, folder)):
            if name.endswith(".py"):
                with open(os.path.join(root, folder, name), encoding="utf-8") as f:
                    actions.update(pattern.findall(f.read()))
    assert len(actions) > 100
    unmapped = sorted(
        a for a in actions
        if a not in notifications.IGNORED_BOARD_ACTIONS and notifications.board_group_for_action(a) is None
    )
    assert unmapped == []


def test_exact_action_wins_over_section_prefix():
    assert notifications.board_group_for_action("garage.control_meter_attach") == "electricity"
    assert notifications.board_group_for_action("garage.edit") == "garages"
    assert notifications.board_group_for_action("person.revision_submit") == "approvals"
    assert notifications.board_group_for_action("person.edit") == "members"
    assert notifications.board_group_for_action("bank_api.balance_sync") is None


# ---------------------------------------------------------------------------
# Выписка банка
# ---------------------------------------------------------------------------

def _sync_statement(db, client, monkeypatch, lines):
    account = make_bank_account(db, provider=BankApiProvider.SBERBANK)
    make_credential(db, account)
    db.commit()
    login(client, "chair", "pass12345")
    monkeypatch.setattr(bank_sync, "get_client", lambda acc: _StubClient(statement_result=lines))
    resp = client.post(
        f"/cooperative/bank-accounts/{account.id}/sync-statement",
        data={"date_from": "2026-08-01", "date_to": "2026-08-02"},
    )
    assert resp.status_code == 302


def test_non_empty_statement_notifies_board_but_not_actor_or_member(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    _board_user(db, "board1")
    _member(db)
    _sync_statement(db, client, monkeypatch, [
        StatementLine(external_uid="u1", operation_date=dt.date(2026, 8, 1), direction="debit", amount=Decimal("50.00")),
    ])

    assert _recipients(fake) == ["board1@example.com"]
    assert "Банк и выписки" in fake.sent[0]["Subject"]
    assert "Загружена выписка" in fake.sent[0].get_content()


def test_empty_statement_sends_nothing(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    _board_user(db, "board1")
    _sync_statement(db, client, monkeypatch, [])
    assert fake.sent == []


def test_disabled_group_sends_nothing(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    _board_user(db, "board1", groups=["finance"])
    _sync_statement(db, client, monkeypatch, [
        StatementLine(external_uid="u1", operation_date=dt.date(2026, 8, 1), direction="debit", amount=Decimal("50.00")),
    ])
    assert fake.sent == []


def test_rolled_back_action_sends_nothing(app, db, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1")
    db.commit()

    with app.test_request_context():
        audit.record("garage.edit", "Изменены данные гаража №1")
        db.rollback()
        notifications.dispatch_after_commit()
        assert fake.sent == []

        audit.record("garage.edit", "Изменены данные гаража №1")
        db.commit()
        notifications.dispatch_after_commit()
    assert _recipients(fake) == ["board1@example.com"]


# ---------------------------------------------------------------------------
# Входы
# ---------------------------------------------------------------------------

def test_login_notifies_only_when_logins_group_enabled(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board_default")
    _board_user(db, "board_logins", groups=["logins"])
    _member(db)
    db.commit()

    login(client, "member1", "pass12345")
    assert _recipients(fake) == ["board_logins@example.com"]
    assert "Входы на сайт" in fake.sent[0]["Subject"]


def test_failed_login_notifies_by_default(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1")
    _member(db)
    db.commit()

    login(client, "member1", "wrong-password")
    assert _recipients(fake) == ["board1@example.com"]
    assert "Неудачные попытки входа" in fake.sent[0]["Subject"]


# ---------------------------------------------------------------------------
# Ждёт решения
# ---------------------------------------------------------------------------

def test_profile_revision_notifies_only_chairman(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    _board_user(db, "board1")
    _member(db)
    db.commit()

    login(client, "member1", "pass12345")
    fake.sent.clear()
    resp = client.post("/cabinet/profile", data={"email": "new@example.com", "phones": ""})
    assert resp.status_code == 302
    assert _recipients(fake) == ["chair@example.com"]
    assert "Ждёт решения правления" in fake.sent[0]["Subject"]


def test_proposal_notifies_board(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    _board_user(db, "board1")
    _member(db)
    db.commit()

    login(client, "member1", "pass12345")
    fake.sent.clear()
    resp = client.post("/proposals/new", data={"title": "Покрасить ворота", "description": ""})
    assert resp.status_code == 302
    assert sorted(_recipients(fake)) == ["board1@example.com", "chair@example.com"]
    assert "Покрасить ворота" in fake.sent[0]["Subject"]


# ---------------------------------------------------------------------------
# Уведомления членам
# ---------------------------------------------------------------------------

def _draft_vote(db):
    now = dt.datetime.now()
    vote = Vote(title="Ремонт крыши", status=VoteStatus.DRAFT, opens_at=now, closes_at=now + dt.timedelta(days=14))
    db.add(vote)
    db.flush()
    db.add(VoteQuestion(vote_id=vote.id, order=1, text="Одобрить смету?"))
    db.commit()
    return vote


def test_vote_open_notifies_voters_once(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    board = _board_user(db, "board1")
    make_ownership(db, make_garage(db, number="b1"), board.person)
    _member(db, "owner1", "owner1@example.com")
    _member(db, "tenant1", "tenant1@example.com", owner=False)
    vote = _draft_vote(db)

    login(client, "chair", "pass12345")
    fake.sent.clear()
    client.post(f"/voting/{vote.id}/open")

    # Член правления — собственник, но получает одно сообщение (правленческое), а не два.
    assert sorted(_recipients(fake)) == ["board1@example.com", "owner1@example.com"]
    member_msg = next(m for m in fake.sent if m["To"] == "owner1@example.com")
    assert member_msg["Subject"] == "Открыто голосование «Ремонт крыши»"


def test_vote_open_respects_member_subscription(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    user, _ = _member(db, "owner1", "owner1@example.com")
    user.notify_vote = False
    vote = _draft_vote(db)

    login(client, "chair", "pass12345")
    fake.sent.clear()
    client.post(f"/voting/{vote.id}/open")
    assert fake.sent == []


def test_future_meeting_notifies_members_past_does_not(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1", groups=[])
    _member(db)
    db.commit()
    login(client, "board1", "pass12345")
    fake.sent.clear()

    past = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    client.post("/meetings/new", data={"date": past, "agenda": "Отчёт"})
    assert fake.sent == []

    future = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    client.post("/meetings/new", data={"date": future, "agenda": "Выборы правления"})
    assert _recipients(fake) == ["member1@example.com"]
    assert "Выборы правления" in fake.sent[0].get_content()


def test_revision_decision_notifies_submitter(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "chair", role=RoleEnum.CHAIRMAN)
    user, person = _member(db)
    revision = PersonDataRevision(
        person_id=person.id, submitted_by_user_id=user.id,
        fields_snapshot=json.dumps({"email": "member1@example.com", "phones": []}),
        previous_snapshot="{}", status=PersonDataRevisionStatus.PENDING,
    )
    db.add(revision)
    db.commit()

    login(client, "chair", "pass12345")
    fake.sent.clear()
    resp = client.post(f"/persons/persons/{person.id}/revisions/reject/{revision.id}")
    assert resp.status_code == 302
    assert _recipients(fake) == ["member1@example.com"]
    assert fake.sent[0]["Subject"] == "Изменения данных отклонены"


def test_public_document_notifies_members_internal_does_not(app, db, client, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1", groups=[])
    _member(db)
    db.commit()
    login(client, "board1", "pass12345")
    fake.sent.clear()

    form = {"doc_type": "other", "date": "2026-10-01", "title": "Смета на ремонт"}
    client.post("/cooperative/documents/new", data={**form, "is_internal": "on"})
    assert fake.sent == []

    client.post("/cooperative/documents/new", data=form)
    assert _recipients(fake) == ["member1@example.com"]
    assert "Смета на ремонт" in fake.sent[0]["Subject"]


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def test_settings_save_board_groups(app, db, client):
    user = _board_user(db, "board1")
    db.commit()
    assert notifications.board_event_groups(user) == set(notifications.DEFAULT_BOARD_EVENT_GROUPS)

    login(client, "board1", "pass12345")
    client.post("/cabinet/profile/notifications", data={
        "notify_channel": "email", "notify_board_events": ["bank", "logins", "bogus"], "notify_vote": "on",
    })
    db.refresh(user)
    assert notifications.board_event_groups(user) == {"bank", "logins"}
    assert user.notify_vote is True
    assert user.notify_meeting is False


def test_settings_board_groups_ignored_for_member(app, db, client):
    user, _ = _member(db)
    db.commit()
    login(client, "member1", "pass12345")
    client.post("/cabinet/profile/notifications", data={"notify_channel": "email", "notify_board_events": ["bank"]})
    db.refresh(user)
    assert user.notify_board_events is None


def test_profile_page_shows_board_groups_only_to_board(app, db, client):
    _board_user(db, "board1")
    _member(db)
    db.commit()

    login(client, "board1", "pass12345")
    html = client.get("/cabinet/profile").get_data(as_text=True)
    assert 'name="notify_board_events" value="bank"' in html
    assert 'name="notify_vote"' in html

    client.get("/auth/logout")
    login(client, "member1", "pass12345")
    html = client.get("/cabinet/profile").get_data(as_text=True)
    assert 'name="notify_board_events"' not in html
    assert 'name="notify_vote"' in html


# ---------------------------------------------------------------------------
# Сбои фоновых синхронизаций — не чаще раза в сутки на источник
# ---------------------------------------------------------------------------

def test_failure_notified_once_per_day_per_source(app, db, monkeypatch):
    from app.models import FailureNotification
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1")
    _board_user(db, "board_off", groups=["bank"])
    db.commit()

    assert notifications.notify_failure("ewelink", "опрос eWeLink", "таймаут") is True
    assert _recipients(fake) == ["board1@example.com"]
    assert fake.sent[0]["Subject"] == "Сбой: опрос eWeLink"

    assert notifications.notify_failure("ewelink", "опрос eWeLink", "таймаут") is False
    assert notifications.notify_failure("dvr:1", "регистратор", "нет связи") is True
    assert len(fake.sent) == 2

    mark = db.query(FailureNotification).filter_by(source="ewelink").one()
    mark.notified_at -= dt.timedelta(hours=25)
    db.commit()
    assert notifications.notify_failure("ewelink", "опрос eWeLink", "таймаут") is True
    assert len(fake.sent) == 3


def test_dvr_script_notifies_after_consecutive_failures_once_per_recorder(app, db, monkeypatch, tmp_path):
    from tests.test_dvr_snapshot_script import _load_dvr_snapshot_module, make_recorder, make_camera
    module = _load_dvr_snapshot_module()
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1")
    recorder = make_recorder(db)
    make_camera(db, recorder, label="Ворота", channel=1)
    make_camera(db, recorder, label="Двор", channel=2)
    db.commit()

    class FailedResult:
        returncode = 1
        stderr = b"Connection refused"

    monkeypatch.setitem(app.config, "DVR_SNAPSHOT_FOLDER", str(tmp_path))
    monkeypatch.setattr(module, "create_app", lambda: app)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: FailedResult())
    monkeypatch.setattr(module, "_update_combined_snapshot", lambda *a: None)

    # Первые прогоны подряд с ошибкой — ещё шум, не уведомляем.
    for _ in range(notifications.FREQUENT_FAILURE_THRESHOLD - 1):
        assert module.main() == 1
    assert fake.sent == []

    assert module.main() == 1
    assert _recipients(fake) == ["board1@example.com"]
    body = fake.sent[0].get_content()
    assert "Ворота" in body and "Двор" in body

    assert module.main() == 1
    assert len(fake.sent) == 1


def test_success_resets_consecutive_failures(app, db, monkeypatch):
    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1")
    db.commit()
    threshold = notifications.FREQUENT_FAILURE_THRESHOLD

    for _ in range(threshold - 1):
        assert notifications.notify_failure("dvr:1", "регистратор", "нет связи", threshold=threshold) is False
    notifications.reset_failure("dvr:1")
    for _ in range(threshold - 1):
        assert notifications.notify_failure("dvr:1", "регистратор", "нет связи", threshold=threshold) is False
    assert fake.sent == []

    assert notifications.notify_failure("dvr:1", "регистратор", "нет связи", threshold=threshold) is True
    assert "Неудачных попыток подряд: 3" in fake.sent[0].get_content()


def test_ewelink_offline_phase_notifies_per_phase(app, db, monkeypatch):
    import importlib.util
    from app.ewelink.client import PhaseSnapshot
    from tests.test_electricity_monitor import make_ewelink_account, make_phase_device

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("poll_ewelink_script", os.path.join(root, "scripts", "poll_ewelink.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake = _mock_smtp(monkeypatch)
    _make_mailbox_settings(db)
    _board_user(db, "board1")
    make_ewelink_account(db, app_id="app", access_token_encrypted="x", family_id="fam")
    make_phase_device(db, label="Фаза A", ewelink_device_id="a")
    make_phase_device(db, label="Фаза B", ewelink_device_id="b", sort_order=1)
    db.commit()

    online = {"a": True, "b": False}

    class FakeClient:
        tokens = object()

        def list_devices(self, family_id):
            return []

        def get_phase_snapshot(self, device_id, family_id, devices=None):
            return PhaseSnapshot(
                power_w=Decimal("100"), voltage_v=Decimal("220"), current_a=Decimal("0.5"), day_kwh=None,
                month_kwh=None, is_online=online[device_id], sled_online=None, switch_on=None,
            )

    monkeypatch.setattr(module, "create_app", lambda: app)
    monkeypatch.setattr(module, "build_client", lambda account: FakeClient())

    for _ in range(notifications.FREQUENT_FAILURE_THRESHOLD):
        assert module.main() == 0
    assert _recipients(fake) == ["board1@example.com"]
    assert "Фаза B" in fake.sent[0]["Subject"]

    # Фаза B вернулась, пропала фаза A — своё сообщение, не ждёт суток после B.
    online.update(a=False, b=True)
    for _ in range(notifications.FREQUENT_FAILURE_THRESHOLD):
        module.main()
    assert len(fake.sent) == 2
    assert "Фаза A" in fake.sent[1]["Subject"]
