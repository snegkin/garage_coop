#!/usr/bin/env python3
"""
Опрос устройств Sonoff POWCT через облако eWeLink — для запуска по cron раз
в минуту (см. scripts/poll_ewelink.sh и docs/deployment.md, раздел «Автоматизация»).

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
     одному на фазу) и сохранить PowerPhaseReading (для офлайн-устройства —
     только отметку без значений, см. reading_from_snapshot), затем свернуть
     показания старше 3 месяцев в почасовые (compact_old_readings).
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

from app import create_app, database, notifications
from app.models import EWeLinkAccount, PowerPhaseDevice
from app.bank_api import crypto
from app.ewelink import EWeLinkApiError, EWeLinkAuthError
from app.electricity_monitor import (
    RAW_READINGS_KEEP_DAYS, build_client, compact_old_readings, persist_tokens, reading_from_snapshot,
)


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
            notifications.notify_failure(
                "ewelink", "опрос eWeLink", f"Ошибка подключения к eWeLink: {exc}",
                threshold=notifications.FREQUENT_FAILURE_THRESHOLD,
            )
            return 1

        active_devices = (
            database.db_session.query(PowerPhaseDevice)
            .filter_by(is_active=True)
            .all()
        )

        saved, failed = 0, 0
        offline = []
        online_ids, offline_devices = [], []
        device_errors = []
        for phase_device in active_devices:
            try:
                snapshot = client.get_phase_snapshot(phase_device.ewelink_device_id, account.family_id, devices=devices)
            except EWeLinkApiError as exc:
                failed += 1
                device_errors.append(f"{phase_device.label}: {exc}")
                print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                      f"{phase_device.label}: {exc}", file=sys.stderr)
                continue

            database.db_session.add(reading_from_snapshot(phase_device.id, snapshot, _utcnow()))
            saved += 1
            if snapshot.is_online:
                online_ids.append(phase_device.id)
            else:
                offline.append(phase_device.label)
                offline_devices.append((phase_device.id, phase_device.label))

        account.last_poll_at = _utcnow()
        account.last_error = None if failed == 0 else f"{failed} из {len(active_devices)} устройств не опрошены — см. лог"
        database.db_session.commit()
        if device_errors:
            notifications.notify_failure(
                "ewelink", "опрос eWeLink",
                f"Не опрошены {failed} из {len(active_devices)} устройств:\n" + "\n".join(device_errors),
                threshold=notifications.FREQUENT_FAILURE_THRESHOLD,
            )
        else:
            notifications.reset_failure("ewelink")
        # Фаза офлайн — устройство не на связи с облаком: чаще всего
        # пропало питание на этой фазе (Sonoff питается от неё же).
        # Источник — по фазе, не общий: иначе сообщение о второй фазе,
        # пропавшей позже, ждало бы сутки после первой.
        for device_id, label in offline_devices:
            notifications.notify_failure(
                f"power_phase:{device_id}", f"фаза {label} не на связи",
                f"Устройство фазы «{label}» не на связи с облаком eWeLink — возможно, на фазе пропало электропитание.",
                threshold=notifications.FREQUENT_FAILURE_THRESHOLD,
            )
        for device_id in online_ids:
            notifications.reset_failure(f"power_phase:{device_id}")

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Опрошено устройств: {saved}, ошибок: {failed}."
              + (f" Офлайн (нет связи с облаком): {', '.join(offline)}." if offline else ""))

        compacted = compact_old_readings(_utcnow())
        if compacted:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Свёрнуто в почасовые показания старше {RAW_READINGS_KEEP_DAYS} дн.: {compacted} устройство-часов.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
