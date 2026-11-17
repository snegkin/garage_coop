"""
Смайлы в чатах правления и ревизионной комиссии картинками, а не системным
шрифтом (у которого на каждой ОС свой рисунок).

Наборы: «apple» (как в Telegram) и «twitter» (Twemoji) — спрайты 64×64 из
пакетов emoji-datasource-* (app/static/vendor/, см. vendor.json), плюс
«system» — как раньше, шрифтом. Выбор — у каждого в браузере (localStorage,
переключатель внизу панели смайлов, base.html: initChatWidget).

Клетки обоих спрайтов расположены одинаково (одна и та же сетка
emoji-datasource), поэтому карта «символ → клетка» одна — её отдаёт
emoji_sprites() из emoji.json пакета apple. Символ в тексте остаётся
символом (прозрачным, картинка — фоном): копирование, цитата выделением и
мосты в Telegram/MAX работают с обычным текстом, в базу картинки не попадают.

Спрайты — неизменённые файлы пакетов. Рисунки Apple принадлежат Apple
(пакет распространяется под MIT только как код); Twemoji — CC-BY 4.0,
© Twitter, Inc. и др. (подпись — в подсказке переключателя).
"""
import json
import os
from functools import lru_cache

from flask import Response, url_for

from .vendor import VENDOR_DIR, load_manifest, package_dir

SETS = ("apple", "twitter")
CELL = 64       # размер картинки в спрайте
PADDING = 1     # поля вокруг каждой картинки (клетка — 66 px)


def _packages() -> dict:
    return load_manifest()["packages"]


def _codes_to_str(codes: str) -> str:
    return "".join(chr(int(c, 16)) for c in codes.split("-"))


@lru_cache(maxsize=1)
def sprite_map() -> dict:
    """{"cols": число клеток в ряд, "map": {символ: [x, y]}} — по полному
    написанию (unified, с FE0F) и по краткому (non_qualified, без FE0F;
    браузер сам решает, подменять ли такое — см. base.html)."""
    package = _packages()["emoji-datasource-apple"]
    path = os.path.join(VENDOR_DIR, package_dir("emoji-datasource-apple", package["version"]), "emoji.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result: dict[str, list[int]] = {}

    def add(entry: dict) -> None:
        cell = [entry["sheet_x"], entry["sheet_y"]]
        for key in ("unified", "non_qualified"):
            if entry.get(key):
                result.setdefault(_codes_to_str(entry[key]), cell)

    cols = 0
    for entry in data:
        add(entry)
        for variation in (entry.get("skin_variations") or {}).values():
            add(variation)
        cols = max(cols, entry["sheet_x"] + 1)
    return {"cols": cols, "cell": CELL, "padding": PADDING, "map": result}


@lru_cache(maxsize=1)
def _sprite_map_json() -> str:
    return json.dumps(sprite_map(), ensure_ascii=False, separators=(",", ":"))


def sheet_urls() -> dict:
    from flask import current_app
    vendor_url = current_app.jinja_env.globals["vendor_url"]
    return {name: vendor_url(f"emoji-datasource-{name}", "sheet-64.png") for name in SETS}


def init_app(app) -> None:
    def emoji_sprites():
        # Без \u-экранирования и пробелов — втрое меньше, чем у jsonify.
        response = Response(_sprite_map_json(), mimetype="application/json")
        # Адрес меняется вместе с версией пакета (?v=), поэтому кэш — на год.
        response.cache_control.public = True
        response.cache_control.max_age = 365 * 24 * 3600
        return response

    app.add_url_rule("/chat/emoji-sprites.json", "chat_emoji_sprites", emoji_sprites)

    def chat_emoji_config() -> dict:
        return {
            "mapUrl": url_for("chat_emoji_sprites", v=_packages()["emoji-datasource-apple"]["version"]),
            "sheets": sheet_urls(),
        }

    app.jinja_env.globals["chat_emoji_config"] = chat_emoji_config
