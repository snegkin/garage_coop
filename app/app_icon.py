"""
Иконка сайта и PWA-приложения кооператива.

С одного кода работают несколько кооперативов (у каждого своя БД), а
иконки лежали файлами в app/static/ — у всех был знак Восхода. Теперь
председатель загружает одно квадратное изображение на странице «Изменить
сведения о кооперативе» (Cooperative.app_icon_png), а все нужные размеры —
favicon, apple-touch-icon, иконки манифеста PWA (в том числе maskable) —
нарезаются из него на лету (app_icon_png хранится уже приведённым к
квадрату 512×512, нарезка дешёвая и кешируется в памяти по версии).

Пока иконка не загружена — отдаются прежние файлы из app/static/.
Адреса /app-icon/<имя> стабильны (их помнит установленное PWA и service
worker); в разметке к ним добавляется ?v=<версия>, чтобы браузер не
держал старую иконку из кеша после замены.
"""
import datetime as dt
import io
from functools import lru_cache

from flask import Blueprint, Response, abort, current_app, g, jsonify, request, send_from_directory, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from . import database
from .auth import roles_required
from .i18n import translate as _
from .models import Cooperative, RoleEnum

bp = Blueprint("app_icon", __name__, url_prefix="/app-icon")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SOURCE_SIZE = 512
# Безопасная зона maskable-иконки — круг диаметром 80% стороны: знак
# вписываем в неё с небольшим запасом, остальное — фон.
MASKABLE_SCALE = 0.72
BACKGROUND = (255, 255, 255, 255)

# имя файла → (размер, вариант)
VARIANTS = {
    "favicon-16x16.png": (16, "any"),
    "favicon-32x32.png": (32, "any"),
    "apple-touch-icon.png": (180, "opaque"),   # iOS заливает прозрачное чёрным
    "icon-192.png": (192, "any"),
    "icon-512.png": (512, "any"),
    "icon-maskable-512.png": (512, "maskable"),
    "favicon.ico": (48, "ico"),
}


def prepare_icon(data: bytes) -> bytes:
    """Загруженное изображение → квадратный PNG SOURCE_SIZE×SOURCE_SIZE.
    Неквадратное вписывается по центру на прозрачный фон (не обрезается:
    у знака с надписью иначе отрезало бы края). ValueError — не картинка."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(str(exc)) from exc
    bbox = img.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("empty image")
    img = img.crop(bbox)  # прозрачные поля по краям не нужны — знак крупнее
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    square = square.resize((SOURCE_SIZE, SOURCE_SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    square.save(out, "PNG", optimize=True)
    return out.getvalue()


def _on_background(img: Image.Image) -> Image.Image:
    bg = Image.new("RGBA", img.size, BACKGROUND)
    bg.alpha_composite(img)
    return bg.convert("RGB")


@lru_cache(maxsize=64)
def render_variant(source: bytes, name: str) -> bytes:
    """Один из VARIANTS из исходника prepare_icon(). Кеш по содержимому —
    после замены иконки старые записи просто вытесняются."""
    size, kind = VARIANTS[name]
    with Image.open(io.BytesIO(source)) as src:
        src = src.convert("RGBA")
    out = io.BytesIO()
    if kind == "ico":
        src.save(out, "ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        return out.getvalue()
    if kind == "maskable":
        inner = round(size * MASKABLE_SCALE)
        canvas = Image.new("RGBA", (size, size), BACKGROUND)
        mark = src.resize((inner, inner), Image.Resampling.LANCZOS)
        canvas.alpha_composite(mark, ((size - inner) // 2, (size - inner) // 2))
        img = canvas.convert("RGB")
    else:
        img = src.resize((size, size), Image.Resampling.LANCZOS)
        if kind == "opaque":
            img = _on_background(img)
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _coop() -> Cooperative | None:
    return database.db_session.query(Cooperative).first()


@bp.app_template_global("app_icon_url")
def app_icon_url(name: str) -> str:
    """Адрес иконки для разметки — с версией, чтобы смена иконки не
    застревала в кеше браузера."""
    # на странице ~7 ссылок на иконки — версию читаем из БД один раз за запрос
    if "_app_icon_version" not in g:
        coop = _coop()
        g._app_icon_version = coop.app_icon_updated_at if coop else None
    updated = g._app_icon_version
    if updated is None:
        return url_for("app_icon.icon", name=name)
    return url_for("app_icon.icon", name=name, v=int(updated.timestamp()))


@bp.route("/<name>")
def icon(name):
    if name not in VARIANTS:
        abort(404)
    coop = _coop()
    source = coop.app_icon_png if coop else None
    if not source:
        return send_from_directory(current_app.static_folder, name, max_age=3600)
    data = render_variant(source, name)
    mimetype = "image/x-icon" if name.endswith(".ico") else "image/png"
    # С ?v= адрес меняется при каждой замене — можно кешировать надолго;
    # без него (манифест у установленного PWA, service worker) — на час.
    max_age = 31536000 if request.args.get("v") else 3600
    return Response(data, mimetype=mimetype, headers={"Cache-Control": f"public, max-age={max_age}"})


@bp.record_once
def _root_favicon(state):
    # /favicon.ico с корня запрашивают сами браузеры и поисковики, мимо <link>.
    state.app.add_url_rule("/favicon.ico", "favicon", lambda: icon("favicon.ico"))


@bp.route("/upload", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def upload():
    """AJAX: загрузка или удаление (delete=1) иконки. Ответ JSON
    {"ok", "message", "custom", "preview"} — карточка на странице
    обновляет себя сама."""
    coop = _coop()
    if coop is None:
        return jsonify(ok=False, message=_("Сначала заполните информацию о кооперативе.")), 400
    if request.form.get("delete"):
        coop.app_icon_png = None
        coop.app_icon_updated_at = None
        message = _("Иконка сброшена на стандартную.")
    else:
        file = request.files.get("image")
        data = file.read(MAX_UPLOAD_BYTES + 1) if file is not None and file.filename else b""
        if not data:
            return jsonify(ok=False, message=_("Выберите файл с изображением.")), 400
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify(ok=False, message=_("Файл слишком большой (не больше 10 МБ).")), 400
        try:
            coop.app_icon_png = prepare_icon(data)
        except ValueError:
            return jsonify(ok=False, message=_("Не удалось прочитать изображение — загрузите PNG или JPG.")), 400
        coop.app_icon_updated_at = dt.datetime.utcnow()
        message = _("Иконка сохранена. Установленное приложение подхватит её при следующем обновлении манифеста браузером.")
    database.db_session.commit()
    g.pop("_app_icon_version", None)
    return jsonify(ok=True, message=message, custom=coop.app_icon_png is not None,
                   preview=app_icon_url("icon-192.png"), maskable=app_icon_url("icon-maskable-512.png"))
