"""
Регрессионные тесты на закрытые уязвимости (см. историю ревью безопасности
в context.md): CSRF, open redirect через `next`, загрузка произвольных
расширений файлов. В отличие от остальных тестов, здесь CSRF-защита
намеренно ВКЛЮЧЕНА (в conftest.py она выключена ради удобства остальных
тестов) — эти тесты как раз про неё.
"""
import io
import os
import re
import tempfile

import pytest

from app import create_app
from app.models import News, NewsAttachment

from tests.conftest import TestConfig


@pytest.fixture()
def csrf_app():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    upload_dir = tempfile.mkdtemp()

    class _Config(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        UPLOAD_FOLDER = upload_dir
        WTF_CSRF_ENABLED = True

    application = create_app(_Config)
    application.testing = True

    yield application

    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture()
def csrf_client(csrf_app):
    return csrf_app.test_client()


def _seed_chairman(csrf_app):
    from werkzeug.security import generate_password_hash
    from app import database
    from app.models import User, RoleEnum
    with csrf_app.app_context():
        database.db_session.add(User(
            username="chairman", password_hash=generate_password_hash("pw12345"), role=RoleEnum.CHAIRMAN,
        ))
        database.db_session.commit()


def _csrf_token(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "csrf meta tag not found on page"
    return m.group(1)


def test_login_without_csrf_token_rejected(csrf_client, csrf_app):
    _seed_chairman(csrf_app)
    resp = csrf_client.post("/auth/login", data={"username": "chairman", "password": "pw12345"})
    # Обычный редирект (302), не голый 400 — чтобы браузер реально
    # проследовал по нему и увидел flash-сообщение, а не страницу ошибки.
    assert resp.status_code == 302


def test_login_with_csrf_token_succeeds(csrf_client, csrf_app):
    _seed_chairman(csrf_app)
    resp = csrf_client.get("/auth/login")
    token = _csrf_token(resp.get_data(as_text=True))
    resp = csrf_client.post(
        "/auth/login", data={"username": "chairman", "password": "pw12345", "csrf_token": token},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_forged_post_without_token_is_blocked(csrf_client, csrf_app):
    """Симулируем CSRF-атаку: залогиненный пользователь, но запрос идёт без
    токена (как если бы форму отправил чужой сайт)."""
    _seed_chairman(csrf_app)
    resp = csrf_client.get("/auth/login")
    token = _csrf_token(resp.get_data(as_text=True))
    csrf_client.post("/auth/login", data={"username": "chairman", "password": "pw12345", "csrf_token": token})

    resp = csrf_client.post("/finance/fee-types/new", data={"code": "FORGED", "name": "Forged"})
    assert resp.status_code == 302
    # Взнос не должен быть создан — редирект означает отказ, а не выполнение.
    from app import database
    from app.models import FeeType
    with csrf_app.app_context():
        assert database.db_session.query(FeeType).filter_by(code="FORGED").first() is None


@pytest.mark.parametrize("next_value", ["//evil.com", "/\\evil.com", "https://evil.com", "evil.com"])
def test_login_open_redirect_rejected(csrf_client, csrf_app, next_value):
    _seed_chairman(csrf_app)
    resp = csrf_client.get("/auth/login")
    token = _csrf_token(resp.get_data(as_text=True))
    resp = csrf_client.post(
        "/auth/login",
        data={"username": "chairman", "password": "pw12345", "csrf_token": token},
        query_string={"next": next_value},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_login_safe_next_is_respected(csrf_client, csrf_app):
    _seed_chairman(csrf_app)
    resp = csrf_client.get("/auth/login")
    token = _csrf_token(resp.get_data(as_text=True))
    resp = csrf_client.post(
        "/auth/login",
        data={"username": "chairman", "password": "pw12345", "csrf_token": token},
        query_string={"next": "/garages/"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/garages/"


def test_news_attachment_rejects_disallowed_extension(csrf_client, csrf_app):
    _seed_chairman(csrf_app)
    resp = csrf_client.get("/auth/login")
    token = _csrf_token(resp.get_data(as_text=True))
    csrf_client.post("/auth/login", data={"username": "chairman", "password": "pw12345", "csrf_token": token})

    resp = csrf_client.get("/news/new")
    token2 = _csrf_token(resp.get_data(as_text=True))
    data = {
        "title": "t", "body": "b", "csrf_token": token2,
        "attachments": (io.BytesIO(b"<script>alert(1)</script>"), "evil.html"),
    }
    csrf_client.post("/news/new", data=data, content_type="multipart/form-data")

    from app import database
    with csrf_app.app_context():
        item = database.db_session.query(News).filter_by(title="t").first()
        assert item is not None
        attachments = database.db_session.query(NewsAttachment).filter_by(news_id=item.id).all()
        assert len(attachments) == 0


def test_login_rate_limited_after_repeated_attempts():
    """Rate limiting включён отдельно от остальных security-тестов (там он
    выключен через TestConfig.RATELIMIT_ENABLED=False, чтобы не мешать
    остальным сценариям) — здесь проверяем сам факт его работы, на
    отдельном приложении, где он включён с самого создания."""
    import tempfile
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    upload_dir = tempfile.mkdtemp()

    class _RateLimitedConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        UPLOAD_FOLDER = upload_dir
        WTF_CSRF_ENABLED = True
        RATELIMIT_ENABLED = True

    rl_app = create_app(_RateLimitedConfig)
    rl_app.testing = True
    client = rl_app.test_client()

    last_status = None
    for _ in range(15):
        resp = client.get("/auth/login")
        token = _csrf_token(resp.get_data(as_text=True))
        last_status = client.post(
            "/auth/login", data={"username": "nobody", "password": "wrong", "csrf_token": token},
        ).status_code

    assert last_status == 429

    os.close(db_fd)
    os.unlink(db_path)


def test_news_attachment_accepts_allowed_extension(csrf_client, csrf_app):
    _seed_chairman(csrf_app)
    resp = csrf_client.get("/auth/login")
    token = _csrf_token(resp.get_data(as_text=True))
    csrf_client.post("/auth/login", data={"username": "chairman", "password": "pw12345", "csrf_token": token})

    resp = csrf_client.get("/news/new")
    token2 = _csrf_token(resp.get_data(as_text=True))
    data = {
        "title": "t2", "body": "b", "csrf_token": token2,
        "attachments": (io.BytesIO(b"%PDF-1.4 fake"), "protocol.pdf"),
    }
    csrf_client.post("/news/new", data=data, content_type="multipart/form-data")

    from app import database
    with csrf_app.app_context():
        item = database.db_session.query(News).filter_by(title="t2").first()
        assert item is not None
        attachments = database.db_session.query(NewsAttachment).filter_by(news_id=item.id).all()
        assert len(attachments) == 1
