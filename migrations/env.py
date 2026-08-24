import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Позволяет запускать `alembic` из корня проекта и находить пакет `app`/`config.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные моделей проекта — источник правды для autogenerate.
from app.models import Base  # noqa: E402
target_metadata = Base.metadata

# URL берём из того же места, что и само приложение (DATABASE_URL / config.py),
# а не дублируем его в alembic.ini — там он всегда был бы неактуален в проде.
# ВАЖНО: если вызывающий код (app/database.py:run_migrations) уже явно передал
# URL через alembic_cfg.attributes["db_url_override"] — например, чтобы
# мигрировать не боевую, а временную/тестовую БД — используем именно его и
# НЕ перетираем значением по умолчанию из Config. Раньше здесь было
# безусловное config.set_main_option("sqlalchemy.url", Config.SQLALCHEMY_DATABASE_URI),
# из-за чего run_migrations() с кастомным database_uri тихо мигрировал не ту
# БД (актуально для тестов — каждый тест получает свой временный sqlite-файл,
# а env.py всё равно накатывал схему на instance/coop.db).
from config import Config  # noqa: E402

_url_override = config.attributes.get("db_url_override")
config.set_main_option("sqlalchemy.url", _url_override or Config.SQLALCHEMY_DATABASE_URI)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite почти не умеет ALTER TABLE — batch-режим пересобирает таблицу
        # через временную копию (COPY DATA), сохраняя все строки, вместо
        # падения на первой же попытке добавить колонку/FK/constraint.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
