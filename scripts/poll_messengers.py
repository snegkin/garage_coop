#!/usr/bin/env python3
"""
Мессенджеры (Telegram и MAX) по cron раз в минуту: очередь исходящих и
опрос обоих ботов (до ноября 2026 — scripts/poll_telegram.py) (см. scripts/poll_messengers.sh и docs/deployment.md, раздел
«Автоматизация»). По аналогии с scripts/poll_ewelink.py — отдельный
скрипт, не фоновый поток внутри веб-процесса.

Сама логика — в app/telegram_bot.py:poll_and_link_accounts (тот же приём,
что у app/notifications.py:run_board_chat_digest + scripts/board_chat_digest.py):
сообщения "/start <token>", привязывающие Telegram-аккаунт к профилю (см.
app/cabinet.py: telegram_link_start), и /chatid. Перед опросом — разбор
очереди исходящих (app/messenger_outbox.py: process) — общей для Telegram и
MAX, и события бота MAX (app/max_bot.py: poll_updates — привязка аккаунтов
по диплинку и каналы, куда бота добавили). Сбой одного мессенджера не мешает другому.

Запуск вручную:
    cd /path/to/project && python3 scripts/poll_messengers.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app import max_bot
from app import messenger_outbox
from app.telegram_bot import poll_and_link_accounts, TelegramError


def main() -> int:
    app = create_app()
    with app.app_context():
        # Сначала очередь исходящих (новости в группу, уведомления) —
        # app/messenger_outbox.py, повторы при сбоях внутри.
        sent, failed = messenger_outbox.process(app.config["UPLOAD_FOLDER"])
        if sent or failed:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Очередь мессенджеров: отправлено {sent}, не удалось {failed}.")
        try:
            max_bot.poll_updates()
        except max_bot.MaxError as exc:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Ошибка опроса MAX: {exc}", file=sys.stderr)
        try:
            total, linked = poll_and_link_accounts()
        except TelegramError as exc:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                  f"Ошибка опроса Telegram: {exc}", file=sys.stderr)
            return 1

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Обработано апдейтов: {total}, привязано аккаунтов: {linked}.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
