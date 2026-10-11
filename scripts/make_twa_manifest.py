#!/usr/bin/env python3
"""
Готовит проект Android-приложения (TWA) одного кооператива для Bubblewrap —
twa-manifest.json в отдельном каталоге ВНЕ репозитория (там же лежит
ключ подписи — его в git класть нельзя). Подробно — docs/android-app.md.

Данные берутся из живого PWA-манифеста сайта кооператива
(https://<домен>/manifest.webmanifest, app/main.py:web_manifest): название,
цвета, иконки. Кириллический домен переводится в punycode — Android
принимает в intent-фильтре только ASCII.

Использование:
    python3 scripts/make_twa_manifest.py https://гм-восход.рф ru.gmvoshod.app
    python3 scripts/make_twa_manifest.py https://гм-восход.рф ru.gmvoshod.app --out ~/coop-android/gmvoshod

Дальше в каталоге проекта — ключ подписи (keytool) и сборка
(npx @bubblewrap/cli build), см. docs/android-app.md, «Сборка».
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import urlsplit

import requests

BUBBLEWRAP_VERSION = "1.27.0"  # схема twa-manifest.json сверена с @bubblewrap/core этой версии
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def ascii_host(url: str) -> str:
    host = urlsplit(url if "://" in url else f"https://{url}").hostname or ""
    return host.encode("idna").decode("ascii")


def build_twa_manifest(host: str, package: str, web_manifest: dict, version_name: str, version_code: int) -> dict:
    origin = f"https://{host}"
    icons = web_manifest.get("icons", [])

    def icon_url(purpose: str) -> str | None:
        candidates = [i for i in icons if purpose in i.get("purpose", "any").split()]
        if not candidates:
            return None
        best = max(candidates, key=lambda i: int(i.get("sizes", "0x0").split("x")[0]))
        return origin + best["src"]

    theme = web_manifest.get("theme_color", "#0d6efd")
    background = web_manifest.get("background_color", "#ffffff")
    name = web_manifest.get("name") or host
    return {
        "packageId": package,
        "host": host,
        "name": name,
        "launcherName": web_manifest.get("short_name") or name,
        "display": web_manifest.get("display", "standalone"),
        "themeColor": theme,
        "navigationColor": "#000000",
        "navigationColorDark": "#000000",
        "navigationDividerColor": "#00000000",
        "navigationDividerColorDark": "#00000000",
        "backgroundColor": background,
        # Push: уведомления сайта показываются как уведомления самого
        # приложения (notification delegation), разрешение — системное
        # разрешение приложения (на Android 13+ спрашивается при первом запуске).
        "enableNotifications": True,
        "startUrl": web_manifest.get("start_url", "/"),
        "iconUrl": icon_url("any"),
        "maskableIconUrl": icon_url("maskable"),
        "splashScreenFadeOutDuration": 300,
        "signingKey": {"path": "./android.keystore", "alias": "android"},
        "appVersionCode": version_code,
        "appVersion": version_name,
        "shortcuts": [],
        "generatorApp": "bubblewrap-cli",
        "webManifestUrl": f"{origin}/manifest.webmanifest",
        "fallbackType": "customtabs",
        "features": {},
        "alphaDependencies": {"enabled": False},
        "enableSiteSettingsShortcut": True,
        "isChromeOSOnly": False,
        "isMetaQuest": False,
        "fullScopeUrl": f"{origin}/",
        "minSdkVersion": 21,
        "orientation": "default",
        "fingerprints": [],
        "additionalTrustedOrigins": [],
        "retainedBundles": [],
        "protocolHandlers": [],
        "fileHandlers": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="twa-manifest.json для Android-приложения кооператива (Bubblewrap).")
    parser.add_argument("url", help="адрес сайта кооператива, например https://гм-восход.рф")
    parser.add_argument("package", help="имя пакета приложения, например ru.gmvoshod.app (после публикации не меняется)")
    parser.add_argument("--out", help="каталог проекта (по умолчанию ~/coop-android/<домен>)")
    parser.add_argument("--version-name", default="1.0.0")
    parser.add_argument("--version-code", type=int, default=1)
    args = parser.parse_args()

    if not _PACKAGE_RE.match(args.package):
        print(f"Некорректное имя пакета: {args.package} — нужны латинские буквы/цифры через точку, "
              f"например ru.gmvoshod.app.", file=sys.stderr)
        return 1
    host = ascii_host(args.url)
    out_dir = os.path.expanduser(args.out or os.path.join("~", "coop-android", host))
    target = os.path.join(out_dir, "twa-manifest.json")
    if os.path.exists(target):
        print(f"{target} уже есть — новую версию собирают в этом каталоге командой "
              f"`npx @bubblewrap/cli update --appVersionName=…` (см. docs/android-app.md).", file=sys.stderr)
        return 1

    resp = requests.get(f"https://{host}/manifest.webmanifest", timeout=20)
    resp.raise_for_status()
    twa = build_twa_manifest(host, args.package, resp.json(), args.version_name, args.version_code)
    if not twa["iconUrl"]:
        print("В манифесте сайта нет иконок — проверьте /manifest.webmanifest.", file=sys.stderr)
        return 1

    os.makedirs(out_dir, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(twa, f, ensure_ascii=False, indent=2)
    print(f"Готово: {target}")
    print(f"Дальше: ключ подписи и сборка в {out_dir} — см. docs/android-app.md, «Сборка».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
