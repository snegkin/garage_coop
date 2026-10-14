"""
Сторонние библиотеки фронтенда (app/static/vendor/) — пути к их файлам для
шаблонов.

Единственный источник правды о версиях — манифест app/static/vendor/vendor.json
(его ведёт scripts/vendor.py: проверка обновлений и уязвимостей, обновление,
восстановление, сверка файлов). Шаблоны не пишут версию в пути сами, а зовут
vendor_url("bootstrap", "bootstrap.min.css") — поэтому обновление библиотеки
не требует правки шаблонов: меняется только манифест и папка с файлами.
"""
import json
import os

VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "vendor")
MANIFEST_PATH = os.path.join(VENDOR_DIR, "vendor.json")


def load_manifest(path: str = MANIFEST_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def package_dir(name: str, version: str) -> str:
    """Имя папки пакета: <пакет>-<версия>; у пакетов с областью (@scope/name)
    — без @ и с «-» вместо «/»."""
    return f"{name.lstrip('@').replace('/', '-')}-{version}"


def init_app(app) -> None:
    # Flask — здесь, а не наверху модуля: scripts/vendor.py грузит этот файл
    # ради путей и package_dir() и должен работать на голом Python, без venv.
    from flask import url_for

    packages = load_manifest()["packages"]

    def vendor_url(name: str, filename: str) -> str:
        package = packages[name]
        if filename not in package["files"]:
            raise KeyError(f"{filename} нет в манифесте пакета {name} (app/static/vendor/vendor.json)")
        return url_for("static", filename=f"vendor/{package_dir(name, package['version'])}/{filename}")

    app.jinja_env.globals["vendor_url"] = vendor_url
