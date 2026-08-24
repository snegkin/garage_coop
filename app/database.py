"""Инициализация БД: движок и scoped_session, подключаемые к жизненному циклу запроса Flask."""
import os

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import scoped_session, sessionmaker

engine = None
db_session = None


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

    db_session = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
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


def init_app(app):
    """Регистрирует закрытие сессии по завершении запроса."""
    @app.teardown_appcontext
    def remove_session(exception=None):
        if db_session is not None:
            db_session.remove()
