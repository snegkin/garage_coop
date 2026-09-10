#!/usr/bin/env python3
"""
Подсчёт непрочитанных писем в почте правления — для запуска по cron (см.
scripts/poll_mailbox.sh и README.md, раздел «Автоматизация»).

Сама логика — в app/mailbox.py:refresh_unread_counts (та же схема, что и у
app/notifications.py:run_board_chat_digest + scripts/board_chat_digest.py):
отдельный cron-скрипт, а не поход на IMAP/POP3 при каждом заходе на сайт —
подробности (почему нельзя считать на каждый HTTP-запрос, как эмулируется
статус для POP3, бутстрап истории) см. в докстринге той функции.

Запуск вручную:
    cd /path/to/project && python3 scripts/poll_mailbox.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.mailbox import refresh_unread_counts


def main() -> int:
    app = create_app()
    with app.app_context():
        message = refresh_unread_counts()
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}")
        return 1 if message.startswith("Ошибка подключения") else 0


if __name__ == "__main__":
    sys.exit(main())
