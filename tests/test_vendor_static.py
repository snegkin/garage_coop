"""
Сторонние библиотеки фронтенда — со своего сервера (app/static/vendor/,
см. README там же), не с CDN: страница не зависит от стороннего сервиса и
не отдаёт ему адреса посетителей.
"""
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = glob.glob(os.path.join(ROOT, "app", "templates", "**", "*.html"), recursive=True)


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


def test_vendor_paths_in_templates_exist():
    missing = []
    for path in TEMPLATES:
        for rel in re.findall(r"filename='(vendor/[^']+)'", open(path, encoding="utf-8").read()):
            if not os.path.isfile(os.path.join(ROOT, "app", "static", rel)):
                missing.append(rel)
    assert missing == []


def test_vendor_files_cached_long_other_static_not(app, client):
    resp = client.get("/static/vendor/bootstrap-5.3.3/bootstrap.min.css")
    assert resp.status_code == 200
    assert "max-age=31536000" in resp.headers.get("Cache-Control", "")
    resp.close()

    resp = client.get("/static/vendor/bootstrap-icons-1.13.2/fonts/bootstrap-icons.woff2")
    assert resp.status_code == 200
    resp.close()

    resp = client.get("/static/logo.png")
    assert "max-age=31536000" not in resp.headers.get("Cache-Control", "")
    resp.close()
