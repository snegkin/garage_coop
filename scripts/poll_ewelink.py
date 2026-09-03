#!/usr/bin/env python3
"""
Опрос устройств Sonoff POWCT через облако eWeLink — для запуска по cron раз
в минуту (см. scripts/poll_ewelink.sh и README.md, раздел «Автоматизация»).

По аналогии с scripts/update_key_rate.py (ставка ЦБ РФ): отдельный скрипт,
а не фоновый поток внутри веб-процесса — не тянет за собой планировщик
(APScemuler и т.п.) как отдельную зависимость, переживает перезапуск
веб-процесса, и его проще перезапустить/остановить независимо от сайта.

Логика:
  1. Взять единственную запись EWeLinkAccount; если подключение не
     настроено (нет app_id/токена/family_id — авторизация и выбор дома
     проходят только через браузер, см. app/electricity_monitor.py) —
     тихо выйти с кодом 0 (это ожидаемое состояние до того, как
     председатель настроит раздел «Мониторинг электропитания», не ошибка).
  2. Запросить list_devices(family_id); при EWeLinkAuthError один раз
     попробовать refresh() и повторить.
  3. Для каждого активного PowerPhaseDevice разобрать снимок из уже
     полученного списка устройств (один HTTP-запрос на весь цикл, не по
     одному на фазу) и сохранить PowerPhaseReading.
  4. Сохранить токены (в т.ч. если eWeLink их ротировал), last_poll_at,
     last_error — независимо от того, успешно ли получилось для всех 3
     устройств (частичный успех — тоже успех, см. per-device try/except
     ниже).

Запуск вручную:
    cd /path/to/project && python3 scripts/poll_ewelink.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.models import EWeLinkAccount, PowerPhaseDevice, PowerPhaseReading
from app.bank_api import crypto
from app.ewelink import EWeLinkApiError, EWeLinkAuthError
from app.electricity_monitor import build_client, persist_tokens


def _utcnow() -> dt.datetime:
    # Returns a naive datetime representing UTC, matching legacy behavior exactly
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def main() -> int:
    app = create_app()
    with app.app_context():
        account = database.db_session.query(EWeLinkAccount).first()
        if account is None or not (account.app_id and account.access_token_encrypted and account.family_id):
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Подключение к eWeLink ещё не настроено — опрос пропущен.")
            return 0

        client = build_client(account)
        if client is None or client.tokens is None:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Не все данные подключения к eWeLink заполнены — опрос пропущен.")
            return 0

        try:
            try:
                devices = client.list_devices(account.family_id)
            except EWeLinkAuthError:
                client.refresh()
                persist_tokens(account, client)
                database.db_session.commit()
                devices = client.list_devices(account.family_id)
        except EWeLinkApiError as exc:
            account.last_error = str(exc)
            account.last_poll_at = _utcnow()
            database.db_session.commit()
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Ошибка подключения к eWeLink: {exc}", file=sys.stderr)
            return 1

        active_devices = (
            database.db_session.query(PowerPhaseDevice)
            .filter_by(is_active=True)
            .all()
        )

        saved, failed = 0, 0
        for phase_device in active_devices:
            try:
                snapshot = client.get_phase_snapshot(phase_device.ewelink_device_id, account.family_id, devices=devices)
            except EWeLinkApiError as exc:
                failed += 1
                print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                      f"{phase_device.label}: {exc}", file=sys.stderr)
                continue

            database.db_session.add(PowerPhaseReading(
                device_id=phase_device.id,
                ts=_utcnow(),
                power_w=snapshot.power_w,
                voltage_v=snapshot.voltage_v,
                current_a=snapshot.current_a,
                day_kwh=snapshot.day_kwh,
                month_kwh=snapshot.month_kwh,
                is_online=snapshot.is_online,
                sled_online=snapshot.sled_online,
                switch_on=snapshot.switch_on,
            ))
            saved += 1

        account.last_poll_at = _utcnow()
        account.last_error = None if failed == 0 else f"{failed} из {len(active_devices)} устройств не опрошены — см. лог"
        database.db_session.commit()

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Опрошено устройств: {saved}, ошибок: {failed}.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
