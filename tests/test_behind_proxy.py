"""
BEHIND_PROXY=1 (config.py, app/__init__.py: ProxyFix) — приложение за nginx
(см. INSTALL.md) берёт адрес и схему посетителя из X-Forwarded-*.
"""
def test_behind_proxy_trusts_forwarded_headers(_migrated_db_template, tmp_path):
    """BEHIND_PROXY=1 — адрес и схема посетителя из заголовков nginx;
    без него заголовки игнорируются (их мог бы подделать кто угодно)."""
    import shutil
    from flask import request
    from app import create_app
    from tests.conftest import TestConfig

    for behind, expected in ((True, ("203.0.113.7", "https")), (False, ("127.0.0.1", "http"))):
        db_path = tmp_path / f"proxy_{behind}.db"
        shutil.copyfile(_migrated_db_template, db_path)

        class C(TestConfig):
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
            UPLOAD_FOLDER = str(tmp_path)
            BEHIND_PROXY = behind

        app = create_app(C)
        seen = {}

        @app.route("/_whoami")
        def _whoami():
            seen["addr"], seen["scheme"] = request.remote_addr, request.scheme
            return ""

        app.test_client().get("/_whoami", headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"},
                              environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert (seen["addr"], seen["scheme"]) == expected
