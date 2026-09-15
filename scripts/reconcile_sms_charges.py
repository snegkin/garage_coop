#!/usr/bin/env python3
"""
Начисляет на личный счёт члена кооператива (вид взноса "telecom_disputes",
FeeType.per_garage=False) реальную стоимость платного SMS с кодом для
восстановления пароля (см. app/auth.py:forgot_password, предупреждение о
цене на странице восстановления пароля) — для запуска по cron (см.
scripts/reconcile_sms_charges.sh и README.md, раздел «Автоматизация»).

Сама отправка (app/sms/__init__.py:_LoggingSmsClient.send) стоимость не
знает — SMS Aero отдаёт её не сразу, а некоторое время спустя, через
отдельный запрос по id сообщения (см. app/sms/smsaero.py:get_message_cost
— ВНИМАНИЕ: этот эндпоинт не подтверждён живым запросом, см. её докстринг,
перед боевым использованием стоит проверить на реальном аккаунте). Поэтому
отправка только помечается в SmsLog (purpose="password_reset",
provider_message_id, charge_id ещё NULL), а начисление создаётся здесь —
отдельным шагом, обычно на следующий запуск после отправки.

Не начисляет дважды: запись SmsLog.charge_id заполняется сразу при
успешном начислении, следующий запуск такую запись уже не трогает
(фильтр charge_id IS NULL). Если провайдер ещё не посчитал стоимость
(get_message_cost вернул None) или временно недоступен (SmsError) —
запись просто остаётся необработанной до следующего запуска, без счётчика
попыток и таймаута: раз в сутки/чаще этого достаточно, стоимость рано или
поздно появляется.

Запуск вручную:
    cd /path/to/project && python3 scripts/reconcile_sms_charges.py

Обычно запускается через обёртку scripts/reconcile_sms_charges.sh (лог,
venv), которую и добавляют в crontab.
"""
import os
import sys

# Путь к корню проекта — на случай запуска не из рабочей директории проекта.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as dt

from app import create_app, database
from app import audit
from app.accounting import reallocate_member_charges
from app.models import SmsLog, SmsLogStatus, MemberAccount, FeeType, Charge, Person
from app.sms import get_sms_client
from app.sms.base import SmsError


def main() -> int:
    app = create_app()
    with app.app_context():
        client = get_sms_client()
        if client is None:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"SMS-провайдер не настроен — нечего сверять.")
            return 0

        pending = (
            database.db_session.query(SmsLog)
            .filter(
                SmsLog.purpose == "password_reset",
                SmsLog.status == SmsLogStatus.SENT,
                SmsLog.charge_id.is_(None),
                SmsLog.person_id.isnot(None),
                SmsLog.provider_message_id.isnot(None),
            )
            .order_by(SmsLog.id)
            .all()
        )
        if not pending:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] Нечего сверять — очередь пуста.")
            return 0

        fee_type = database.db_session.query(FeeType).filter_by(code="telecom_disputes").first()
        if fee_type is None:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Вид взноса telecom_disputes не найден — миграция не применена?", file=sys.stderr)
            return 1

        charged = 0
        not_ready = 0
        errors = 0
        for log in pending:
            try:
                cost = client.get_message_cost(log.provider_message_id)
            except (SmsError, AttributeError) as exc:
                errors += 1
                print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                      f"SmsLog#{log.id}: не удалось узнать стоимость ({exc}) — попробуем в следующий раз.")
                continue
            if cost is None:
                not_ready += 1
                continue
            if cost <= 0:
                # Провайдер отдал нулевую/отрицательную стоимость — не с чего
                # взыскивать, просто помечаем обработанной без начисления
                # (charge_id остаётся NULL, но раз cost не None, а <= 0 — это
                # не "ещё не готово", повторный запрос его не изменит; чтобы
                # не спрашивать бесконечно, создаём нулевое начисление ниже
                # не будем — вместо этого просто не трогаем запись и logируем).
                not_ready += 1
                print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                      f"SmsLog#{log.id}: провайдер отдал нулевую/отрицательную стоимость ({cost}) — пропущено.")
                continue

            account = (
                database.db_session.query(MemberAccount)
                .filter_by(person_id=log.person_id, fee_type_id=fee_type.id, garage_id=None, is_archived=False)
                .first()
            )
            if account is None:
                errors += 1
                print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                      f"SmsLog#{log.id}: у person_id={log.person_id} нет личного счёта telecom_disputes — пропущено.")
                continue

            charge = Charge(
                account_id=account.id, year=dt.date.today().year, amount=cost,
                comment="Восстановление пароля по SMS",
            )
            database.db_session.add(charge)
            database.db_session.flush()
            reallocate_member_charges(account)
            log.charge_id = charge.id
            person = database.db_session.get(Person, log.person_id)
            audit.record(
                "sms.password_reset_charged", entity_type="member_account", entity_id=account.id,
                summary=f"Начислена стоимость SMS восстановления пароля {audit.format_amount(cost)} "
                        f"на счёт {account.account_number} ({person.short_name if person else log.person_id})",
            )
            database.db_session.commit()
            charged += 1

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Начислено: {charged}, ещё не готово (нет стоимости у провайдера): {not_ready}, ошибок: {errors} "
              f"(всего в очереди: {len(pending)}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
