#!/usr/bin/env python3
"""
Подсчёт непрочитанных писем в почте правления — для запуска по cron (см.
scripts/poll_mailbox.sh и README.md, раздел «Автоматизация»).

По аналогии с poll_ewelink.py: отдельный cron-скрипт, а не поход на IMAP
при каждом заходе на сайт. Почта правления нигде не кэшируется (см.
app/mailbox.py) — открытие IMAP-соединения может занять до
mail_client.CONNECT_TIMEOUT секунд или зависнуть при недоступном сервере,
а бейдж "непрочитано" в шапке (app/__init__.py: _inject_user) рендерится
на КАЖДОЙ странице для каждого члена правления. Здесь результат
(MailboxSettings.unread_count) считается раз в несколько минут и просто
читается веб-процессом одним дешёвым SELECT, без сети.

Только IMAP — у POP3 нет флага "прочитано" протокольно (см.
mail_client.IncomingMailClient.supports_flags), для него unread_count
остаётся 0. Web-интерфейс (mailbox.inbox()) обновляет тот же кэш
опортунистически при каждом заходе во "Входящие" — этот скрипт лишь
гарантирует, что бейдж появится и без захода на сайт.

Запуск вручную:
    cd /path/to/project && python3 scripts/poll_mailbox.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database, mail_client
from app.models import MailboxSettings, MailProtocol
from app.mail_client import MailError


def main() -> int:
    app = create_app()
    with app.app_context():
        settings = database.db_session.query(MailboxSettings).first()
        if settings is None or not (settings.incoming_host and settings.username and settings.password_encrypted):
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Почта правления ещё не настроена — опрос пропущен.")
            return 0

        if settings.incoming_protocol != MailProtocol.IMAP:
            settings.unread_count = 0
            settings.last_checked_at = dt.datetime.utcnow()
            database.db_session.commit()
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"POP3 не поддерживает флаг «прочитано» — опрос пропущен.")
            return 0

        try:
            with mail_client.get_incoming_client(settings) as client:
                unread = client.count_unread(mail_client.DEFAULT_FOLDER)
        except MailError as exc:
            settings.last_error = str(exc)
            settings.last_checked_at = dt.datetime.utcnow()
            database.db_session.commit()
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Ошибка подключения к почте: {exc}", file=sys.stderr)
            return 1

        settings.unread_count = unread
        settings.last_error = None
        settings.last_checked_at = dt.datetime.utcnow()
        database.db_session.commit()

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] Непрочитанных писем: {unread}.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
