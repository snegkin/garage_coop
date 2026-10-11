"""scripts/make_twa_manifest.py — twa-manifest.json для Bubblewrap из PWA-манифеста."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from make_twa_manifest import ascii_host, build_twa_manifest  # noqa: E402

WEB_MANIFEST = {
    "name": "ГМ Восход", "short_name": "Восход", "start_url": "/", "display": "standalone",
    "background_color": "#ffffff", "theme_color": "#0d6efd",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "purpose": "any"},
        {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "purpose": "maskable"},
    ],
}


def test_cyrillic_domain_becomes_punycode():
    assert ascii_host("https://гм-восход.рф/") == "xn----dtbbg1boax0b.xn--p1ai"
    assert ascii_host("example.com") == "example.com"


def test_twa_manifest_from_web_manifest():
    twa = build_twa_manifest("example.com", "ru.example.coop", WEB_MANIFEST, "1.0.0", 1)
    assert twa["packageId"] == "ru.example.coop" and twa["host"] == "example.com"
    assert twa["name"] == "ГМ Восход" and twa["launcherName"] == "Восход"
    assert twa["iconUrl"] == "https://example.com/static/icon-512.png"  # самая большая из "any"
    assert twa["maskableIconUrl"] == "https://example.com/static/icon-maskable-512.png"
    assert twa["enableNotifications"] is True  # push — уведомления самого приложения
    assert twa["signingKey"] == {"path": "./android.keystore", "alias": "android"}
