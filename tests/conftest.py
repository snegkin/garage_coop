"""
Общие фикстуры для тестов.

Каждый тест получает чистую БД (временный файл SQLite, накатанный через
Alembic, как в реальном приложении — не Base.metadata.create_all(), чтобы
тесты ловили и ошибки в самих миграциях). Сами миграции прогоняются ОДИН
раз за сессию в шаблонную БД (_migrated_db_template), каждому тесту
достаётся копия файла — раньше все ~70 миграций (с batch_alter_table —
пересозданием таблиц в SQLite) накатывались заново на каждый тест, это
~10 с на тест. create_app() на копии всё равно вызывает run_migrations(),
но на БД, уже стоящей на head, это no-op. CSRF в тестовом конфиге выключен
(WTF_CSRF_ENABLED=False) — специально для проверки самой CSRF-защиты есть
отдельный конфиг в tests/test_security.py, здесь она бы только мешала
писать тесты на бизнес-логику.
"""
import os
import shutil
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from config import Config
from app import database
from app.database import run_migrations
from app.models import Person, User, RoleEnum, Garage, GarageOwnership


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    SECRET_KEY = "test-secret-key"


@pytest.fixture(scope="session")
def _migrated_db_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("db_template") / "template.db"
    run_migrations(f"sqlite:///{path}")
    return path


@pytest.fixture()
def app(_migrated_db_template):
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    shutil.copyfile(_migrated_db_template, db_path)
    upload_dir = tempfile.mkdtemp()

    class _Config(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        UPLOAD_FOLDER = upload_dir

    application = create_app(_Config)
    application.testing = True

    yield application

    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    """Даёт доступ к db_session внутри app_context — большинство тестов
    бизнес-логики работают с моделями напрямую, без похода через HTTP."""
    with app.app_context():
        yield database.db_session


def make_person(db_session, full_name="Тестовый Человек", **kwargs):
    person = Person(full_name=full_name, **kwargs)
    db_session.add(person)
    db_session.flush()
    return person


def make_garage(db_session, number="1", area_sqm="18.00", **kwargs):
    garage = Garage(number=number, area_sqm=area_sqm, **kwargs)
    db_session.add(garage)
    db_session.flush()
    return garage


def make_ownership(db_session, garage, person, share="1"):
    ownership = GarageOwnership(garage_id=garage.id, person_id=person.id, share=share)
    db_session.add(ownership)
    db_session.flush()
    return ownership


def make_user(db_session, username, password, role=RoleEnum.MEMBER, person=None):
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        person_id=person.id if person else None,
    )
    db_session.add(user)
    db_session.flush()
    return user


def login(client, username, password):
    return client.post("/auth/login", data={"username": username, "password": password})
