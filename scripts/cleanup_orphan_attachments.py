#!/usr/bin/env python3
"""
Очистка «осиротевших» inline-вложений — картинок, загруженных через кнопку
«Вставить картинку» в форме новости/вики/объявления доски/сообщения
форума (AJAX, ещё до сохранения самой статьи/страницы/объявления/
сообщения — см. app/news.py: upload_inline_attachment, app/wiki.py:
upload_inline_attachment, app/bulletin.py: upload_inline_attachment,
app/forum.py: upload_inline_attachment), для которых так и не случилось
сохранение с упоминанием этой картинки в тексте (черновик закрыли не
сохранив, картинку вставили и передумали, вкладку закрыли и т.п.).

Такое вложение в БД отличимо однозначно: news_id (page_id для вики,
post_id для доски объявлений и для форума) IS NULL. Если статья/страница/
объявление/сообщение сохраняется — _sync_inline_attachments() «забирает»
его (проставляет FK), см. app/news.py, app/wiki.py, app/bulletin.py,
app/forum.py. Значит IS NULL спустя разумный запас времени после загрузки
= точно не забрано = можно удалять.

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

Там же — осиротевшие файлы клиентских сертификатов банка в
BANK_CERTS_FOLDER (cleanup_bank_certs): PEM с именем, которое выдаёт
app/bank_sync.py (<uuid>.cert.pem/<uuid>.key.pem), но на которое не
ссылается ни одна BankApiCredential. Так бывает, если упал commit после
загрузки нового сертификата или БД заменили копией с другой машины.
Порог тот же — файлу больше 24 часов (по mtime), чтобы не задеть загрузку,
которая прямо сейчас ещё не закоммичена. Остальные файлы каталога не
трогаются.

Запуск вручную:
    cd /path/to/project && python3 scripts/cleanup_orphan_attachments.py

Обычно — через scripts/cleanup_orphan_attachments.sh (лог, venv, flock) по
cron, см. docs/deployment.md, раздел «Автоматизация».
"""
import datetime as dt
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database
from app.models import NewsAttachment, WikiAttachment, BulletinAttachment, ForumAttachment, BankApiCredential

ORPHAN_MAX_AGE = dt.timedelta(hours=24)
_CERT_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(cert|key)\.pem$")


def cleanup_bank_certs(certs_dir: str, referenced: set[str], max_age: dt.timedelta = ORPHAN_MAX_AGE,
                       now: float | None = None) -> list[str]:
    """Удаляет из certs_dir сгенерированные PEM без ссылки из БД, старше
    max_age; возвращает имена удалённых."""
    if not os.path.isdir(certs_dir):
        return []
    cutoff = (now if now is not None else time.time()) - max_age.total_seconds()
    removed = []
    for name in sorted(os.listdir(certs_dir)):
        if not _CERT_NAME_RE.match(name) or name in referenced:
            continue
        path = os.path.join(certs_dir, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            removed.append(name)
    return removed


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
        bulletin_orphans = (
            database.db_session.query(BulletinAttachment)
            .filter(BulletinAttachment.post_id.is_(None), BulletinAttachment.created_at < cutoff)
            .all()
        )
        forum_orphans = (
            database.db_session.query(ForumAttachment)
            .filter(ForumAttachment.post_id.is_(None), ForumAttachment.created_at < cutoff)
            .all()
        )

        for att in news_orphans:
            database.db_session.delete(att)
        for att in wiki_orphans:
            database.db_session.delete(att)
        for att in bulletin_orphans:
            database.db_session.delete(att)
        for att in forum_orphans:
            database.db_session.delete(att)

        database.db_session.commit()

        referenced = set()
        for cert_name, key_name in database.db_session.query(
            BankApiCredential.tls_cert_filename, BankApiCredential.tls_key_filename,
        ):
            referenced.update(name for name in (cert_name, key_name) if name)
        removed_certs = cleanup_bank_certs(app.config["BANK_CERTS_FOLDER"], referenced)

        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
              f"Удалено осиротевших вложений: новости — {len(news_orphans)}, "
              f"вики — {len(wiki_orphans)}, доска объявлений — {len(bulletin_orphans)}, "
              f"форум — {len(forum_orphans)}, файлы сертификатов банка — {len(removed_certs)} "
              f"(старше {ORPHAN_MAX_AGE}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
