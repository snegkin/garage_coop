"""
Пути к данным экземпляра сайта для скриптов, которые работают с файлом БД
напрямую, без create_app() (backup_db.py, restore_db.py, repair_db.py) —
по тем же правилам, что и config.py, чтобы скрипт и веб-процесс одного
кооператива гарантированно смотрели в одну и ту же БД.

Раньше эти скрипты знали только DATABASE_PATH и по умолчанию брали
<проект>/instance/coop.db, а приложение — DATABASE_URL: при втором
кооперативе с одного checkout'а (см. docs/deployment.md, «Несколько
кооперативов на одном сервере») бэкап с .env, где задан только
DATABASE_URL, тихо копировал бы БД ПЕРВОГО кооператива.
"""
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.environ.get("INSTANCE_DIR") or os.path.join(PROJECT_DIR, "instance")


def db_path() -> str:
    """DATABASE_PATH (явное переопределение) → путь из sqlite-URL в
    DATABASE_URL → INSTANCE_DIR/coop.db (как в config.py)."""
    if os.environ.get("DATABASE_PATH"):
        return os.environ["DATABASE_PATH"]
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return url.removeprefix("sqlite:///")
    return os.path.join(INSTANCE_DIR, "coop.db")


def backup_dir() -> str:
    return os.environ.get("BACKUP_DIR") or os.path.join(INSTANCE_DIR, "backups")
