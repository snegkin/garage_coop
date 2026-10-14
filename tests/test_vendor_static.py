"""
Сторонние библиотеки фронтенда — со своего сервера (app/static/vendor/,
манифест vendor.json, ведёт scripts/vendor.py; см. README там же), не с CDN:
страница не зависит от стороннего сервиса и не отдаёт ему адреса посетителей.
"""
import glob
import hashlib
import json
import os
import re

from app.vendor import MANIFEST_PATH, VENDOR_DIR, package_dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = glob.glob(os.path.join(ROOT, "app", "templates", "**", "*.html"), recursive=True)
PACKAGES = json.load(open(MANIFEST_PATH, encoding="utf-8"))["packages"]


def test_templates_do_not_load_from_cdn():
    offenders = []
    for path in TEMPLATES:
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r'(?:src|href)="(https?://[^"]+\.(?:js|mjs|css))"', text):
            offenders.append(f"{os.path.relpath(path, ROOT)}: {m.group(1)}")
        if "cdn.jsdelivr" in text or "unpkg.com" in text or "cdnjs" in text:
            offenders.append(os.path.relpath(path, ROOT))
    # reCAPTCHA — единственное исключение: локально её не разместить.
    offenders = [o for o in offenders if "google.com/recaptcha" not in o]
    assert offenders == []


def test_templates_use_vendor_url_not_hardcoded_versions():
    """Версия пакета — только в манифесте: иначе после обновления шаблон
    ссылался бы на удалённую папку."""
    hardcoded = [
        os.path.relpath(p, ROOT) for p in TEMPLATES
        if re.search(r"filename='vendor/", open(p, encoding="utf-8").read())
    ]
    assert hardcoded == []


def test_vendor_url_calls_match_manifest():
    unknown = []
    for path in TEMPLATES:
        for name, filename in re.findall(r"vendor_url\('([^']+)', '([^']+)'\)", open(path, encoding="utf-8").read()):
            if name not in PACKAGES or filename not in PACKAGES[name]["files"]:
                unknown.append(f"{os.path.relpath(path, ROOT)}: {name}/{filename}")
    assert unknown == []


def test_vendor_files_match_manifest_checksums():
    """То же, что `python3 scripts/vendor.py verify`: файлы не правили руками,
    лишних папок нет."""
    problems = []
    for name, package in PACKAGES.items():
        folder = os.path.join(VENDOR_DIR, package_dir(name, package["version"]))
        for relative, expected in package["sha256"].items():
            path = os.path.join(folder, relative)
            if not os.path.isfile(path):
                problems.append(f"нет {name}/{relative}")
            elif hashlib.sha256(open(path, "rb").read()).hexdigest() != expected:
                problems.append(f"изменён {name}/{relative}")
    expected_dirs = {package_dir(n, p["version"]) for n, p in PACKAGES.items()}
    actual_dirs = {d for d in os.listdir(VENDOR_DIR) if os.path.isdir(os.path.join(VENDOR_DIR, d))}
    problems += [f"лишняя папка {d}" for d in sorted(actual_dirs - expected_dirs)]
    assert problems == []


def test_source_maps_are_served(app, client):
    """Карты исходников — чтобы инструменты разработчика не сыпали 404."""
    for name, package in PACKAGES.items():
        folder = package_dir(name, package["version"])
        for relative in package["files"]:
            content = open(os.path.join(VENDOR_DIR, folder, relative), "rb").read()
            match = re.search(rb"sourceMappingURL=([^\s*'\"]+)", content[-2000:])
            if not match or b":" in match.group(1):
                continue
            map_path = os.path.normpath(os.path.join(os.path.dirname(relative), match.group(1).decode()))
            resp = client.get(f"/static/vendor/{folder}/{map_path}")
            assert resp.status_code == 200, f"{folder}/{map_path}"
            resp.close()


def test_vendor_files_cached_long_other_static_not(app, client):
    folder = package_dir("bootstrap", PACKAGES["bootstrap"]["version"])
    resp = client.get(f"/static/vendor/{folder}/bootstrap.min.css")
    assert resp.status_code == 200
    assert "max-age=31536000" in resp.headers.get("Cache-Control", "")
    resp.close()

    resp = client.get("/static/logo.png")
    assert "max-age=31536000" not in resp.headers.get("Cache-Control", "")
    resp.close()


def test_pages_render_vendor_urls(app, db, client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert f"/static/vendor/{package_dir('bootstrap', PACKAGES['bootstrap']['version'])}/bootstrap.min.css" in html
    assert "bootstrap-icons.min.css" in html

