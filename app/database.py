"""Инициализация БД: движок и scoped_session, подключаемые к жизненному циклу запроса Flask."""
import contextlib
import fcntl
import logging
import os
import re

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker

logger = logging.getLogger(__name__)

engine = None
db_session = None

# Длина строковых колонок (String(n)): SQLite её не проверяет и молча
# сохраняет строку любой длины — так в базе оказывались, например, р/с с
# пробелами (24 символа в String(20)) и КПП с пробелом впереди. На другой
# СУБД такая запись упала бы. Проверяем сами перед каждым flush: в тестах
# (STRICT_STRING_LENGTHS = True, см. tests/conftest.py) превышение — ошибка,
# в работе — предупреждение в лог (данные не теряем, но узнаём о проблеме).
STRICT_STRING_LENGTHS = False


class StringTooLongError(ValueError):
    pass


def _check_string_lengths(session, _flush_context, _instances):
    for obj in list(session.new) + list(session.dirty):
        mapper = inspect(obj).mapper
        for column in mapper.columns:
            length = getattr(column.type, "length", None)
            if not length:
                continue
            value = getattr(obj, mapper.get_property_by_column(column).key, None)
            if isinstance(value, str) and len(value) > length:
                message = (f"{mapper.class_.__name__}.{column.key}: строка длиной {len(value)} "
                           f"не помещается в колонку на {length} символов: {value[:60]!r}")
                if STRICT_STRING_LENGTHS:
                    raise StringTooLongError(message)
                logger.warning(message)


def init_engine(database_uri: str):
    global engine, db_session
    engine = create_engine(database_uri, connect_args={"check_same_thread": False})

    if database_uri.startswith("sqlite"):
        # SQLite по умолчанию НЕ проверяет внешние ключи — без этого события
        # ondelete=CASCADE/SET NULL и защита от удаления связанных строк не работают.
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL (Write-Ahead Logging) вместо журнала по умолчанию (rollback
            # journal) — читатели (просмотр страниц) не блокируют писателя
            # (сохранение платежа/начисления) и наоборот. Для кооператива,
            # где председатель/бухгалтер/правление могут работать
            # одновременно, это заметно снижает "database is locked" при
            # обычной SQLite-блокировке на запись. Настройка постоянная
            # (сохраняется в самом файле БД), но выставляем на каждое
            # подключение — дёшево и не зависит от того, кто создал файл.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    # Отложенные уведомления — уходят только после commit, см.
    # app/notifications.py: after_commit.
    from . import notifications
    event.listen(session_factory, "after_commit", notifications.on_session_commit)
    event.listen(session_factory, "after_rollback", notifications.on_session_rollback)
    event.listen(session_factory, "before_flush", _check_string_lengths)
    db_session = scoped_session(session_factory)
    return engine, db_session


def run_migrations(database_uri: str):
    """Приводит схему БД к актуальной ревизии Alembic, не трогая данные.

    Заменяет старый `Base.metadata.create_all()`, который умел только
    создавать отсутствующие таблицы, но не мог добавить новую колонку/FK
    к уже существующей — из-за чего раньше при каждом изменении моделей
    приходилось удалять instance/coop.db и делать seed заново.

    Три сценария при старте приложения:
    1. Совсем новая БД (файла/таблиц ещё нет) — `alembic upgrade head`
       создаёт всю схему через миграции, история сразу консистентна.
    2. "Старая" БД, заведённая ещё старым create_all() и не знающая про
       Alembic (нет таблицы alembic_version), но уже содержащая таблицу
       user — значит, реальные данные кооператива. Её схема на момент
       перехода совпадает с baseline-миграцией, поэтому её не накатывают
       заново (упадёт на "таблица уже существует"), а просто помечают
       текущей ревизией — `alembic stamp head`.
    3. БД уже под управлением Alembic — обычный `alembic upgrade head`
       применит только новые миграции поверх текущих данных.
    """
    # Alembic на INFO пишет «setup plugin…» (при импорте), «Context impl…/
    # Will assume…» (на каждое подключение) при КАЖДОМ запуске — а
    # create_app() вызывает каждый cron-скрипт, раз в минуту. Глушим на
    # время миграций; о реально применённых сообщает одна строка в
    # _run_migrations.
    # Быстрый путь: схема уже на последней ревизии (почти каждый запуск) —
    # Alembic не загружаем вовсе. Его upgrade() даже вхолостую импортирует все
    # ~80 файлов миграций — ~1,5 с на каждый запуск cron-скрипта.
    if _is_at_head(database_uri):
        return
    # Миграции накатывает тот процесс, что стартовал первым: веб-воркер или
    # cron-скрипт (они тоже вызывают create_app()) — после git pull до
    # перезапуска сервиса это бывает одновременно. batch_alter_table на
    # SQLite не транзакционный: второй процесс, начавший ту же миграцию,
    # оставлял _alembic_tmp_<таблица>, и все следующие старты падали на
    # «table _alembic_tmp_… already exists». Блокировка файла — остальные
    # ждут, затем видят, что схема уже на последней ревизии.
    with _migration_lock(database_uri):
        if _is_at_head(database_uri):
            return
        alembic_logger = logging.getLogger("alembic")
        previous_level = alembic_logger.level
        alembic_logger.setLevel(logging.WARNING)
        try:
            _drop_stale_batch_tables(database_uri)
            _run_migrations(database_uri)
        finally:
            alembic_logger.setLevel(previous_level)


def _sqlite_path(database_uri: str) -> str | None:
    """Путь к файлу SQLite из URI или None (другая СУБД, БД в памяти)."""
    prefix = "sqlite:///"
    if not database_uri.startswith(prefix):
        return None
    path = database_uri[len(prefix):].split("?", 1)[0]
    return None if not path or path == ":memory:" else path


@contextlib.contextmanager
def _migration_lock(database_uri: str):
    """Межпроцессная блокировка на время миграций: файл <БД>.migrate.lock
    рядом с базой (flock снимается и при падении процесса)."""
    path = _sqlite_path(database_uri)
    if path is None:
        yield
        return
    with open(path + ".migrate.lock", "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _drop_stale_batch_tables(database_uri: str) -> None:
    """Временные таблицы Alembic (_alembic_tmp_<таблица>), оставшиеся от
    прерванной миграции. Под блокировкой миграций других миграторов нет —
    такая таблица точно мусор, если исходная таблица на месте (batch
    сначала копирует данные во временную и только потом удаляет исходную).
    Исходной нет — данные могли остаться только во временной: не трогаем,
    миграция упадёт с понятной ошибкой, разбираться руками."""
    if _sqlite_path(database_uri) is None:
        return
    probe_engine = create_engine(database_uri, connect_args={"check_same_thread": False})
    try:
        with probe_engine.begin() as conn:
            tables = set(inspect(conn).get_table_names())
            for name in sorted(t for t in tables if t.startswith("_alembic_tmp_")):
                original = name[len("_alembic_tmp_"):]
                if original in tables:
                    conn.execute(text(f'DROP TABLE "{name}"'))
                    logger.warning("Удалена временная таблица прерванной миграции: %s", name)
                else:
                    logger.error("Временная таблица %s есть, а исходной %s нет — нужна ручная проверка",
                                 name, original)
    finally:
        probe_engine.dispose()


_REVISION_RE = re.compile(r"^revision\b[^=]*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_REVISION_RE = re.compile(r"^down_revision\b[^=]*=\s*(.+)$", re.MULTILINE)


def _head_revisions() -> set[str]:
    """Head-ревизии по тексту файлов миграций (revision/down_revision), без
    их импорта: ревизии, на которые не ссылается ни один down_revision."""
    versions_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations", "versions")
    revisions, parents = set(), set()
    for name in os.listdir(versions_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(versions_dir, name), encoding="utf-8") as f:
            source = f.read()
        rev = _REVISION_RE.search(source)
        down = _DOWN_REVISION_RE.search(source)
        if rev is None or down is None:
            return set()  # непривычный формат файла — пусть разбирается сам Alembic
        revisions.add(rev.group(1))
        parents.update(re.findall(r"['\"]([^'\"]+)['\"]", down.group(1)))
    return revisions - parents


def _is_at_head(database_uri: str) -> bool:
    heads = _head_revisions()
    if not heads:
        return False
    probe_engine = create_engine(database_uri, connect_args={"check_same_thread": False})
    try:
        with probe_engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return False
            current = {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}
    finally:
        probe_engine.dispose()
    return current == heads


def _run_migrations(database_uri: str):
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    alembic_cfg = AlembicConfig(os.path.join(project_root, "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", database_uri)
    # attributes — штатный канал Alembic для передачи данных из вызывающего
    # кода в env.py (см. migrations/env.py: config.attributes.get("db_url_override")).
    # set_main_option() выше недостаточно сам по себе: env.py исторически
    # безусловно перезаписывал sqlalchemy.url значением из Config, поэтому
    # нужен отдельный, более приоритетный канал передачи URL.
    alembic_cfg.attributes["db_url_override"] = database_uri
    alembic_cfg.attributes["configure_logger"] = False

    probe_engine = create_engine(database_uri, connect_args={"check_same_thread": False})
    try:
        existing_tables = set(inspect(probe_engine).get_table_names())
        with probe_engine.connect() as conn:
            current_rev = MigrationContext.configure(conn).get_current_revision()
    finally:
        probe_engine.dispose()

    if current_rev is None and "user" in existing_tables:
        # Сценарий 2: старая БД с данными, ещё не знающая про Alembic.
        command.stamp(alembic_cfg, "head")
    else:
        # Сценарий 1 или 3.
        command.upgrade(alembic_cfg, "head")

    probe_engine = create_engine(database_uri, connect_args={"check_same_thread": False})
    try:
        with probe_engine.connect() as conn:
            new_rev = MigrationContext.configure(conn).get_current_revision()
    finally:
        probe_engine.dispose()
    if new_rev != current_rev:
        logger.info("Схема БД обновлена: ревизия %s → %s", current_rev or "(пусто)", new_rev)


def init_app(app):
    """Регистрирует закрытие сессии по завершении запроса и отправку
    уведомлений о закоммиченных за запрос действиях (до remove() — иначе
    очередь пропадёт вместе с сессией)."""
    @app.after_request
    def dispatch_notifications(response):
        from . import notifications
        notifications.dispatch_after_commit()
        return response

    @app.teardown_appcontext
    def remove_session(exception=None):
        if db_session is not None:
            db_session.remove()
