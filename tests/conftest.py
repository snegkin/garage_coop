"""
Общие фикстуры для тестов.

Каждый тест получает чистую БД (временный файл SQLite, накатанный через
Alembic, как в реальном приложении — не Base.metadata.create_all(), чтобы
тесты ловили и ошибки в самих миграциях). CSRF в тестовом конфиге выключен
(WTF_CSRF_ENABLED=False) — специально для проверки самой CSRF-защиты есть
отдельный конфиг в tests/test_security.py, здесь она бы только мешала
писать тесты на бизнес-логику.
"""
import os
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from config import Config
from app import database
from app.models import Person, User, RoleEnum, Garage, GarageOwnership


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    SECRET_KEY = "test-secret-key"


@pytest.fixture()
def app():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
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
