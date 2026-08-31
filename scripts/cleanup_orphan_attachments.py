#!/usr/bin/env python3
"""
Очистка «осиротевших» inline-вложений — картинок, загруженных через кнопку
«Вставить картинку» в форме новости/вики (AJAX, ещё до сохранения самой
статьи/страницы — см. app/news.py: upload_inline_attachment, app/wiki.py:
upload_inline_attachment), для которых так и не случилось сохранение с
упоминанием этой картинки в тексте (черновик закрыли не сохранив, картинку
вставили и передумали, вкладку закрыли и т.п.).

Такое вложение в БД отличимо однозначно: news_id (или page_id для вики)
IS NULL. Если статья/страница сохраняется — _sync_inline_attachments()
«забирает» его (проставляет FK), см. app/news.py и app/wiki.py. Значит
IS NULL спустя разумный запас времени после загрузки = точно не забрано =
можно удалять.

Порог — 24 часа с момента загрузки (created_at), не сразу: правление может
писать статью долго, с перерывами, вкладка может быть открыта день —
удалять раньше означало бы ломать картинку в открытой форме, которую вот-вот
сохранят. У самой картинки при попытке сохранить статью после того, как
cron её уже удалил, просто отвалится ссылка в тексте (![](битая ссылка)) —
крайне маловероятный, но не катастрофичный случай (не ошибка 500, не потеря
данных статьи).

Удаление через ORM (database.db_session.delete), не сырой SQL — чтобы
сработал event-listener _delete_attachment_file (см. app/models.py),
убирающий сам файл с диска, а не только строку в БД.

Запуск вручную:
    cd /path/to/project && python3 scripts/cleanup_orphan_attachments.py

Обычно — через scripts/cleanup_orphan_attachments.sh (лог, venv, flock) по
cron, см. README.md, раздел «Автоматизация».
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.models import NewsAttachment, WikiAttachment

ORPHAN_MAX_AGE = dt.timedelta(hours=24)


def main() -> int:
    app = create_app()
    with app.app_context():
        cutoff = dt.datetime.utcnow() - ORPHAN_MAX_AGE

        news_orphans = (
            database.db_session.query(NewsAttachment)
            .filter(NewsAttachment.news_id.is_(None), NewsAttachment.created_at < cutoff)
            .all()
        )
        wiki_orphans = (
            database.db_session.query(WikiAttachment)
            .filter(WikiAttachment.page_id.is_(None), WikiAttachment.created_at < cutoff)
            .all()
        )

        for att in news_orphans:
            database.db_session.delete(att)
        for att in wiki_orphans:
            database.db_session.delete(att)

        database.db_session.commit()

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Удалено осиротевших вложений: новости — {len(news_orphans)}, "
              f"вики — {len(wiki_orphans)} (старше {ORPHAN_MAX_AGE}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
