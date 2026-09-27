"""
Интервал автоматического обновления баланса личного кабинета контрагента
(Counterparty.balance_sync_interval_days, app/counterparty_sync.py:
balance_sync_due, scripts/sync_bank_accounts.py) — чтобы ежедневный cron
не давал запись в журнале аудита на каждый день у контрагентов, чей баланс
меняется постоянно (SMS Aero).
"""
import datetime as dt
import importlib.util
import os

from app.counterparty_sync import balance_sync_due
from app.models import RoleEnum, Counterparty, CounterpartyApiProvider

from tests.conftest import make_user, login

TODAY = dt.date(2026, 9, 27)


def _counterparty(interval, updated_on=None):
    return Counterparty(
        name="SMS Aero", api_provider=CounterpartyApiProvider.SMSAERO, balance_sync_interval_days=interval,
        # время позже, чем cron в следующий раз — сравнение по дням, не по часам
        external_balance_updated_at=dt.datetime.combine(updated_on, dt.time(23, 59)) if updated_on else None,
    )


def test_never_synced_is_due():
    assert balance_sync_due(_counterparty(30), TODAY)


def test_daily_is_due_next_day_regardless_of_time():
    assert not balance_sync_due(_counterparty(1, TODAY), TODAY)
    assert balance_sync_due(_counterparty(1, TODAY - dt.timedelta(days=1)), TODAY)


def test_weekly_waits_seven_days():
    assert not balance_sync_due(_counterparty(7, TODAY - dt.timedelta(days=6)), TODAY)
    assert balance_sync_due(_counterparty(7, TODAY - dt.timedelta(days=7)), TODAY)


def _load_sync_script():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "sync_bank_accounts.py")
    spec = importlib.util.spec_from_file_location("sync_bank_accounts_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cron_skips_counterparties_within_interval(app, db, monkeypatch):
    now = dt.datetime.utcnow()
    due = Counterparty(
        name="Ежедневный", api_provider=CounterpartyApiProvider.SMSAERO, balance_sync_interval_days=1,
        external_balance_updated_at=now - dt.timedelta(days=1),
    )
    not_due = Counterparty(
        name="Еженедельный", api_provider=CounterpartyApiProvider.BEGET, balance_sync_interval_days=7,
        external_balance_updated_at=now - dt.timedelta(days=2),
    )
    db.add_all([due, not_due])
    db.commit()

    script = _load_sync_script()
    synced = []
    monkeypatch.setattr(script, "sync_counterparty_balance", lambda cp: synced.append(cp.name) or ("success", "ok"))
    assert script._sync_counterparties() is False
    assert synced == ["Ежедневный"]


def test_edit_form_saves_interval_and_rejects_unknown_values(app, db, client):
    counterparty = Counterparty(name="ООО Ромашка", api_provider=CounterpartyApiProvider.SMSAERO)
    db.add(counterparty)
    make_user(db, "board_int", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_int", "pass12345")
    url = f"/counterparties/{counterparty.id}/edit"
    form = {"name": "ООО Ромашка", "api_provider": "smsaero"}

    client.post(url, data={**form, "balance_sync_interval_days": "7"})
    db.expire_all()
    assert db.get(Counterparty, counterparty.id).balance_sync_interval_days == 7

    client.post(url, data={**form, "balance_sync_interval_days": "5"})  # не из списка
    db.expire_all()
    assert db.get(Counterparty, counterparty.id).balance_sync_interval_days == 1

    html = client.get(f"/counterparties/{counterparty.id}").get_data(as_text=True)
    assert 'name="balance_sync_interval_days"' in html
