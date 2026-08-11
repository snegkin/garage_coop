"""Инициализация БД: движок и scoped_session, подключаемые к жизненному циклу запроса Flask."""
from sqlalchemy import create_engine, event
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
            cursor.close()

    db_session = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
    return engine, db_session


def init_app(app):
    """Регистрирует закрытие сессии по завершении запроса."""
    @app.teardown_appcontext
    def remove_session(exception=None):
        if db_session is not None:
            db_session.remove()
