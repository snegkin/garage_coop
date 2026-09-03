#!/usr/bin/env python3
"""
Автоматическое начисление пени по просроченным взносам/налогу — для запуска
по cron РАЗ В МЕСЯЦ (см. scripts/accrue_penalty.sh и README.md, раздел
«Автоматизация»).

Раньше accrue_penalties() вызывалась тихо на каждом открытии дашборда или
страницы «Пеня» (app/main.py/app/penalty.py) — от этого отказались: с одной
стороны, начисление происходило неочевидно для правления (просто зашёл на
сайт — и уже что-то начислилось, без явного действия и уведомления), с
другой — при частых заходах в систему история начислений раздувалась почти
построчно на каждый день просрочки, вместо одной строки на разумный период.
Теперь accrue_penalties() вызывается ТОЛЬКО отсюда, раз в месяц — то же
самое итоговое начисление (день-в-день, по факту непогашенного остатка и
ставке ЦБ РФ на каждый день), но одной строкой за период с прошлого запуска,
а не десятками. См. app/penalty.py:accrue_penalties — сама она всё равно
считает пеню день за днём внутри периода, здесь не теряется точность, только
частота создания новых записей начислений.

Запуск вручную:
    cd /path/to/project && python3 scripts/accrue_penalty.py

Обычно запускается через обёртку scripts/accrue_penalty.sh (лог, venv),
которую и добавляют в crontab.
"""
import datetime as dt
import os
import sys

# Путь к корню проекта — на случай запуска не из рабочей директории проекта.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.penalty import accrue_penalties


def main() -> int:
    app = create_app()
    with app.app_context():
        result = accrue_penalties(dt.date.today())

        if result.get("error") == "no_due_date":
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Срок оплаты взносов не настроен (реквизиты кооператива) — начисление пропущено.", file=sys.stderr)
            return 1
        if result.get("error") == "no_key_rate":
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Нет ни одной записи ключевой ставки ЦБ РФ — начисление пропущено. "
                  f"Проверьте scripts/update_key_rate.py.", file=sys.stderr)
            return 1

        charged = len(result["charged_rows"])
        skipped = len(result["skipped_rows"])
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Начислено пени: {charged} на сумму {result['total']} ₽" +
              (f", пропущено (нет счёта пени): {skipped}" if skipped else "") + ".")
        return 0


if __name__ == "__main__":
    sys.exit(main())
