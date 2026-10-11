"""Быстрый путь run_migrations (app/database.py): head по тексту файлов
миграций должен совпадать с тем, что считает сам Alembic."""
import os

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from app import database


def test_head_revisions_match_alembic():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = ScriptDirectory.from_config(AlembicConfig(os.path.join(root, "alembic.ini")))
    assert database._head_revisions() == set(script.get_heads())


def test_is_at_head_only_for_migrated_db(tmp_path):
    uri = f"sqlite:///{tmp_path / 'coop.db'}"
    assert database._is_at_head(uri) is False  # пустая БД — нужен полный прогон
    database.run_migrations(uri)
    assert database._is_at_head(uri) is True
