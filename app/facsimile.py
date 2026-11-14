"""
Факсимиле: печать кооператива и подпись председателя на печатных формах.

Изображения загружает председатель (страница «Информация о кооперативе»
→ «Изменить»): печать — одна на кооператив (Cooperative.seal_png), подпись
— своя, на его карточке человека (Person.signature_png): при смене
председателя чужая подпись в документы не попадает. При загрузке фон
скана делается прозрачным, поля обрезаются (prepare_image) — иначе белый
прямоугольник закрывал бы линию подписи. Хранятся в БД (PNG) — попадают
в бэкап вместе с базой, отдельной раздачи файлов нет: в страницу и PDF
они встраиваются data:-URI и только для председателя.

Вставка — только галочкой «Подпись и печать» на странице документа
(legal_docs/_macros.html: facsimile_toggle): по умолчанию документ
печатается «под живую подпись». Галочка на экране лишь показывает
изображения (класс facsimile-on), а PDF с ними запрашивается параметром
facsimile=1 (галочка дописывает его к ссылкам/формам PDF) — каждый такой
PDF пишется в журнал аудита (legal_docs._render_pdf_or_fallback).
Подпись ставится, только если подписант документа — сам председатель,
который сейчас вошёл.
"""
import base64
import io

from flask import Blueprint, Response, request, redirect, url_for, flash, g
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageMath, ImageOps, UnidentifiedImageError

from . import audit
from . import database
from .auth import roles_required
from .i18n import translate as _
from .models import Cooperative, Person, RoleEnum

bp = Blueprint("facsimile", __name__, url_prefix="/facsimile")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SIDE = 1200          # px — итоговое изображение
WORK_SIDE = 1600         # px — обработка (фото с телефона уменьшается заранее)
SOFT_EDGE = 50           # ширина плавного перехода «чернила → фон» по яркости

# Цвет чернил: None — как на фото; "vivid" — насыщеннее и темнее (бледная
# печать/подпись на фото); кортеж — ровный цвет (самый чистый результат).
COLORS = {
    "original": None,
    "vivid": "vivid",
    "blue": (40, 65, 175),
    "darkblue": (20, 30, 110),
    "black": (25, 25, 25),
}


class Filter:
    """Настройки очистки (форма загрузки, ползунки предпросмотра).

    strength 0–100 — сила очистки фона: больше — светлые места (бумага,
    тени, бледные штрихи) уходят в прозрачность смелее. flatten —
    выровнять освещение (фото: тень от руки/телефона, градиент от лампы).
    despeckle — убрать крапинки (текстура бумаги, шум камеры)."""

    def __init__(self, strength: int = 50, flatten: bool = True, despeckle: bool = True, color: str = "original"):
        self.strength = max(0, min(100, int(strength)))
        self.flatten = flatten
        self.despeckle = despeckle
        self.color = color if color in COLORS else "original"

    @classmethod
    def from_form(cls, form) -> "Filter":
        try:
            strength = int(form.get("strength", 50))
        except (TypeError, ValueError):
            strength = 50
        return cls(strength=strength, flatten=form.get("flatten", "1") == "1",
                   despeckle=form.get("despeckle", "1") == "1", color=form.get("color", "original"))


def _paper_background(gray: Image.Image) -> Image.Image:
    """Оценка яркости бумаги в каждой точке: на уменьшенной копии штрихи
    «закрашиваются» соседним светлым (MaxFilter), остаются тени и
    градиент освещения; размытие и обратное увеличение — гладкий фон."""
    w, h = gray.size
    # Уменьшение в 4 раза и морфологическое закрытие (MaxFilter, затем
    # MinFilter того же размера): тонкие тёмные штрихи до ~24 px толщиной
    # (ободок печати) «закрашиваются» светлым, а край большой тени
    # остаётся на месте — один MaxFilter сдвинул бы его на тёмную сторону,
    # и вдоль тени осталась бы полоса «чернил».
    small = gray.resize((max(1, w // 4), max(1, h // 4)), Image.Resampling.BOX)
    small = small.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7)).filter(ImageFilter.GaussianBlur(1))
    return small.resize((w, h), Image.Resampling.BILINEAR)


def _paper_level(gray: Image.Image) -> int:
    """Яркость бумаги — 90-й процентиль гистограммы (бумаги на снимке
    больше, чем чернил)."""
    hist = gray.histogram()
    total, acc = sum(hist), 0
    for level, count in enumerate(hist):
        acc += count
        if acc >= total * 0.9:
            return max(level, 1)
    return 255


def prepare_image(data: bytes, flt: Filter | None = None) -> bytes:
    """Скан/фото подписи или печати → PNG с прозрачным фоном, без полей,
    не больше MAX_SIDE по длинной стороне. ValueError — не картинка или
    после очистки ничего не осталось."""
    flt = flt or Filter()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("not an image") from exc
    img = ImageOps.exif_transpose(img).convert("RGBA")
    img.thumbnail((WORK_SIDE, WORK_SIDE))
    source_alpha = img.getchannel("A")
    rgb = img.convert("RGB")
    gray = rgb.convert("L")

    # Яркость относительно бумаги: бумага → ~255 и в тени, и на свету.
    if flt.flatten:
        background = _paper_background(gray)
        norm = ImageMath.lambda_eval(
            lambda a: a["convert"](a["min"](a["g"] * 255 / (a["b"] + 1), 255), "L"), g=gray, b=background)
    else:
        level = _paper_level(gray)
        norm = gray.point(lambda v: min(255, v * 255 // level))

    hi = 245 - flt.strength * 0.9            # светлее — прозрачно
    lo = hi - SOFT_EDGE                      # темнее — непрозрачно
    alpha = norm.point(lambda v: 255 if v <= lo else 0 if v >= hi else int((hi - v) * 255 / (hi - lo)))
    if flt.despeckle:
        alpha = alpha.filter(ImageFilter.MedianFilter(3))
    alpha = ImageChops.multiply(alpha, source_alpha)

    color = COLORS[flt.color]
    if color in (None, "vivid") and flt.flatten:
        # Цвет чернил — тоже относительно бумаги: в тени штрих не темнее,
        # чем на свету, и без желтизны/серости бумаги.
        rgb = Image.merge("RGB", [
            ImageMath.lambda_eval(
                lambda a: a["convert"](a["min"](a["c"] * 255 / (a["b"] + 1), 255), "L"), c=band, b=background)
            for band in rgb.split()
        ])
    if color is None:
        out = rgb
    elif color == "vivid":
        out = ImageEnhance.Brightness(ImageEnhance.Color(rgb).enhance(1.8)).enhance(0.8)
    else:
        out = Image.new("RGB", rgb.size, color)
    # Под полностью прозрачным — ровный белый, а не шум бумаги: иначе PNG
    # раздувается в разы (шум не сжимается), а видно его всё равно не было.
    out = Image.composite(out, Image.new("RGB", out.size, (255, 255, 255)), alpha.point(lambda v: 255 if v else 0))
    out = out.convert("RGBA")
    out.putalpha(alpha)

    # Рамка — по заметным штрихам: одиночные почти прозрачные точки
    # (крапинки в углу) медианой убираются и поля не раздувают.
    bbox = alpha.point(lambda v: 255 if v > 96 else 0).filter(ImageFilter.MedianFilter(5)).getbbox()
    if bbox is None:
        raise ValueError("empty image")
    pad = 4
    bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(out.width, bbox[2] + pad), min(out.height, bbox[3] + pad))
    out = out.crop(bbox)
    out.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _data_uri(png: bytes | None) -> str | None:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii") if png else None


def _coop() -> Cooperative | None:
    return database.db_session.query(Cooperative).first()


def _is_chairman() -> bool:
    user = getattr(g, "user", None)
    return user is not None and user.role == RoleEnum.CHAIRMAN


def _own_person() -> Person | None:
    user = getattr(g, "user", None)
    return database.db_session.get(Person, user.person_id) if user is not None and user.person_id else None


# ---------------------------------------------------------------------------
# Для шаблонов
# ---------------------------------------------------------------------------

@bp.app_template_global("facsimile_on")
def facsimile_on() -> bool:
    """Запрошена ли вставка (PDF/повторная отрисовка с facsimile=1)."""
    return _is_chairman() and request.values.get("facsimile") == "1"


@bp.app_template_global("facsimile_seal")
def facsimile_seal() -> str | None:
    """data:-URI печати — только председателю."""
    if not _is_chairman():
        return None
    if "facsimile_seal" not in g:
        coop = _coop()
        g.facsimile_seal = _data_uri(coop.seal_png) if coop is not None else None
    return g.facsimile_seal


@bp.app_template_global("facsimile_signature")
def facsimile_signature(signer) -> str | None:
    """data:-URI подписи — только если подписант документа и есть
    вошедший председатель."""
    if not _is_chairman() or signer is None or signer.id != g.user.person_id:
        return None
    if "facsimile_signature" not in g:
        person = _own_person()
        g.facsimile_signature = _data_uri(person.signature_png) if person is not None else None
    return g.facsimile_signature


@bp.app_template_global("facsimile_own_signature")
def facsimile_own_signature() -> str | None:
    """data:-URI подписи вошедшего председателя (для стиля страницы)."""
    if not _is_chairman():
        return None
    person = _own_person()
    return facsimile_signature(person)


@bp.app_template_global("facsimile_available")
def facsimile_available() -> bool:
    """Показывать ли галочку: председатель и что-то загружено."""
    if not _is_chairman():
        return False
    person = _own_person()
    return bool(facsimile_seal() or (person is not None and person.signature_png))


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------

def _read_upload() -> bytes | None:
    file = request.files.get("image")
    if file is None or not file.filename:
        flash(_("Выберите файл с изображением."), "danger")
        return None
    data = file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        flash(_("Файл слишком большой (не больше 10 МБ)."), "danger")
        return None
    try:
        return prepare_image(data, Filter.from_form(request.form))
    except ValueError:
        flash(_("Не удалось прочитать изображение — загрузите PNG или JPG (или ослабьте очистку фона)."), "danger")
        return None


@bp.route("/preview", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def preview():
    """Предпросмотр очистки (ползунки на странице загрузки) — ничего не
    сохраняет, отдаёт PNG; 422 — не картинка/после очистки пусто."""
    file = request.files.get("image")
    data = file.read(MAX_UPLOAD_BYTES + 1) if file is not None else b""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        return Response(status=413 if data else 400)
    try:
        png = prepare_image(data, Filter.from_form(request.form))
    except ValueError:
        return Response(status=422)
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


@bp.route("/seal", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def upload_seal():
    coop = _coop()
    if coop is None:
        flash(_("Сначала заполните информацию о кооперативе."), "warning")
        return redirect(url_for("cooperative.edit"))
    if request.form.get("delete"):
        coop.seal_png = None
        audit.record("facsimile.seal_delete", "Удалено изображение печати кооператива")
        flash(_("Изображение печати удалено."), "success")
    else:
        png = _read_upload()
        if png is None:
            return redirect(url_for("cooperative.edit", _anchor="facsimile"))
        coop.seal_png = png
        audit.record("facsimile.seal_upload", "Загружено изображение печати кооператива")
        flash(_("Изображение печати сохранено."), "success")
    database.db_session.commit()
    return redirect(url_for("cooperative.edit", _anchor="facsimile"))


@bp.route("/signature", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def upload_signature():
    person = _own_person()
    if person is None:
        flash(_("Учётная запись не связана с карточкой человека — подпись некуда сохранить."), "danger")
        return redirect(url_for("cooperative.edit", _anchor="facsimile"))
    if request.form.get("delete"):
        person.signature_png = None
        audit.record("facsimile.signature_delete", f"Удалено изображение подписи: {person.full_name}",
                     entity_type="person", entity_id=person.id)
        flash(_("Изображение подписи удалено."), "success")
    else:
        png = _read_upload()
        if png is None:
            return redirect(url_for("cooperative.edit", _anchor="facsimile"))
        person.signature_png = png
        audit.record("facsimile.signature_upload", f"Загружено изображение подписи: {person.full_name}",
                     entity_type="person", entity_id=person.id)
        flash(_("Изображение подписи сохранено."), "success")
    database.db_session.commit()
    return redirect(url_for("cooperative.edit", _anchor="facsimile"))
