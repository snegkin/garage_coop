#!/usr/bin/env python3
"""
Автоматическое обновление ключевой ставки ЦБ РФ — для запуска по cron
(см. scripts/update_key_rate.sh и README.md, раздел «Автоматизация»).

Подтягивает с cbr.ru всё, что накопилось со дня, следующего за последней
сохранённой записью (или с 13.09.2013 — дата введения ключевой ставки Банком
России, если в БД ещё вообще ничего нет), по сегодня. Само начисление пени
(scripts/accrue_penalty.py) идёт по cron раз в месяц и использует ставку,
актуальную на момент своего запуска, — этот скрипт запускается чаще (раз в
сутки), чтобы к моменту месячного начисления ставка уже точно была свежей
(а не тянулась с cbr.ru в последний момент и не подвела при недоступности
сайта), а также чтобы официальный расчёт пени для отчётов/иска
(persons.penalty_calculation, compute_charge_penalty_breakdown) всегда
опирался на актуальные данные, а не на ставку месячной давности.

Запуск вручную:
    cd /path/to/project && python3 scripts/update_key_rate.py

Обычно запускается через обёртку scripts/update_key_rate.sh (лог, venv),
которую и добавляют в crontab.
"""
import datetime as dt
import os
import sys

# Путь к корню проекта — на случай запуска не из рабочей директории проекта.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.error
import xml.etree.ElementTree as ET

from app import create_app, database
from app.models import KeyRate
from app.penalty import fetch_key_rates_from_cbr, save_key_rates, compact_key_rates

# Дата введения Банком России ключевой ставки как основного индикатора —
# раньше этой даты запрашивать нет смысла, ставки не существовало.
CBR_KEY_RATE_INTRODUCED = dt.date(2013, 9, 13)


def main() -> int:
    app = create_app()
    with app.app_context():
        latest = (
            database.db_session.query(KeyRate)
            .order_by(KeyRate.effective_date.desc())
            .first()
        )
        from_date = (latest.effective_date + dt.timedelta(days=1)) if latest else CBR_KEY_RATE_INTRODUCED
        to_date = dt.date.today()

        if from_date > to_date:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Ключевая ставка уже актуальна (последняя запись — {latest.effective_date}), обновление не требуется.")
            return 0

        try:
            rows = fetch_key_rates_from_cbr(from_date, to_date)
        except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError) as exc:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Ошибка загрузки ставки с cbr.ru ({from_date} — {to_date}): {exc}", file=sys.stderr)
            return 1

        touched = save_key_rates(rows)
        compacted = compact_key_rates()
        database.db_session.commit()

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Загружено/обновлено записей: {touched}, убрано избыточных: {compacted} "
              f"(период {from_date} — {to_date}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
