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

from flask import Blueprint, request, redirect, url_for, flash, g
from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

from . import audit
from . import database
from .auth import roles_required
from .i18n import translate as _
from .models import Cooperative, Person, RoleEnum

bp = Blueprint("facsimile", __name__, url_prefix="/facsimile")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SIDE = 1200
# Яркость (0–255): светлее TRANSPARENT_FROM — фон (полностью прозрачно),
# темнее OPAQUE_UNTIL — чернила (непрозрачно), между — плавный переход,
# чтобы края штрихов не были зубчатыми.
OPAQUE_UNTIL = 160
TRANSPARENT_FROM = 230


def prepare_image(data: bytes) -> bytes:
    """Скан/фото подписи или печати → PNG с прозрачным фоном, без полей,
    не больше MAX_SIDE по длинной стороне. ValueError — не картинка."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("not an image") from exc
    img = ImageOps.exif_transpose(img).convert("RGBA")
    span = TRANSPARENT_FROM - OPAQUE_UNTIL
    alpha = img.convert("L").point(
        lambda v: 255 if v <= OPAQUE_UNTIL else 0 if v >= TRANSPARENT_FROM else (TRANSPARENT_FROM - v) * 255 // span
    )
    alpha = ImageChops.multiply(alpha, img.getchannel("A"))
    img.putalpha(alpha)
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError("empty image")
    img = img.crop(bbox)
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


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
        return prepare_image(data)
    except ValueError:
        flash(_("Не удалось прочитать изображение — загрузите PNG или JPG."), "danger")
        return None


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
