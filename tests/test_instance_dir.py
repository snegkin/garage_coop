"""
INSTANCE_DIR — каталог данных экземпляра сайта (config.py) и те же правила
для скриптов, работающих с файлом БД напрямую (scripts/_paths.py): несколько
кооперативов с одного checkout'а кода, у каждого свой .env.

Значения читаются при импорте модулей из os.environ — проверяем в отдельном
процессе, чтобы не перезагружать config в процессе тестов.
"""
import json
import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE = """
import json, sys
sys.path.insert(0, "scripts")
import warnings; warnings.simplefilter("ignore")
from config import Config
import _paths
print(json.dumps({
    "db_uri": Config.SQLALCHEMY_DATABASE_URI,
    "uploads": Config.UPLOAD_FOLDER,
    "dvr": Config.DVR_SNAPSHOT_FOLDER,
    "certs": Config.BANK_CERTS_FOLDER,
    "script_db": _paths.db_path(),
    "script_backups": _paths.backup_dir(),
}))
"""

_PATH_VARS = (
    "INSTANCE_DIR", "DATABASE_URL", "DATABASE_PATH", "BACKUP_DIR",
    "UPLOAD_FOLDER", "DVR_SNAPSHOT_FOLDER", "BANK_CERTS_FOLDER",
)


def _probe(**env):
    clean_env = {k: v for k, v in os.environ.items() if k not in _PATH_VARS}
    clean_env.update(env)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=PROJECT_DIR, env=clean_env,
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def test_defaults_stay_in_project_instance_dir():
    paths = _probe()
    instance = os.path.join(PROJECT_DIR, "instance")
    assert paths["db_uri"] == f"sqlite:///{instance}/coop.db"
    assert paths["uploads"] == os.path.join(instance, "uploads")
    assert paths["script_db"] == os.path.join(instance, "coop.db")
    assert paths["script_backups"] == os.path.join(instance, "backups")


def test_instance_dir_moves_all_data_paths(tmp_path):
    paths = _probe(INSTANCE_DIR=str(tmp_path))
    assert paths["db_uri"] == f"sqlite:///{tmp_path}/coop.db"
    assert paths["uploads"] == str(tmp_path / "uploads")
    assert paths["dvr"] == str(tmp_path / "dvr")
    assert paths["certs"] == str(tmp_path / "bank_certs")
    assert paths["script_db"] == str(tmp_path / "coop.db")
    assert paths["script_backups"] == str(tmp_path / "backups")


def test_scripts_follow_database_url_like_the_app(tmp_path):
    """Регрессия: backup/restore/repair знали только DATABASE_PATH — с .env,
    где задан лишь DATABASE_URL, бэкап второго кооператива снимался бы с БД
    первого."""
    db_file = tmp_path / "coop2.db"
    paths = _probe(DATABASE_URL=f"sqlite:///{db_file}")
    assert paths["db_uri"] == f"sqlite:///{db_file}"
    assert paths["script_db"] == str(db_file)


def test_database_path_still_overrides_for_scripts(tmp_path):
    paths = _probe(DATABASE_URL=f"sqlite:///{tmp_path}/a.db", DATABASE_PATH=str(tmp_path / "b.db"))
    assert paths["script_db"] == str(tmp_path / "b.db")
