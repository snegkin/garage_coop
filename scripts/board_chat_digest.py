#!/usr/bin/env python3
"""
Уведомление членам правления о непрочитанных сообщениях в чате правления —
для запуска по cron КАЖДЫЕ 5 МИНУТ (см. scripts/board_chat_digest.sh и
README.md, раздел «Автоматизация»).

Сама логика — в app/notifications.py:run_board_chat_digest (тот же приём,
что и у app/penalty.py:accrue_penalties + scripts/accrue_penalty.py):
если последнее сообщение в чате правления старше 10 минут и кто-то из
подписавшихся (User.notify_board_chat) его ещё не прочитал, ему шлётся
уведомление; повторно — только когда появляется НОВОЕ непрочитанное
сообщение (см. User.board_chat_notified_message_id).

Запуск вручную:
    cd /path/to/project && python3 scripts/board_chat_digest.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.notifications import run_board_chat_digest


def main() -> int:
    app = create_app()
    with app.app_context():
        notified = run_board_chat_digest()
        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Уведомлений о непрочитанном чате правления отправлено: {notified}.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
