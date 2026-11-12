"""
Галерея «подручных» картинок для новостей (`/news/gallery/`, модель
NewsGalleryImage): то, что нужно из года в год — субботник, подорожание
электроэнергии, собрание. Правление загружает картинки один раз, а в форме
новости кнопка «Из галереи» (news/form.html) вставляет выбранную в текст.

Вставка — копия файла в новое inline-вложение новости (тот же путь, что у
«Вставить картинку», news.upload_inline_attachment): дальше она живёт как
обычная картинка новости — показ в ленте, альбом в Telegram/MAX, очистка
неиспользованных (scripts/cleanup_orphan_attachments.py). Удаление или
замена картинки в галерее старые новости не трогает.
"""
import datetime as dt
import os
import shutil
import uuid

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_file, jsonify,
)

from . import audit
from . import database
from .auth import roles_required
from .garages import ALLOWED_PHOTO_EXT
from .i18n import translate as _
from .models import NewsAttachment, NewsGalleryImage, RoleEnum
from .uploads import save_upload

bp = Blueprint("news_gallery", __name__, url_prefix="/news/gallery")

TITLE_MAX = 120


def _images() -> list[NewsGalleryImage]:
    """Частые — первыми, среди равных — недавно использованные/добавленные."""
    return (
        database.db_session.query(NewsGalleryImage)
        .order_by(NewsGalleryImage.use_count.desc(),
                  NewsGalleryImage.last_used_at.desc().nulls_last(),
                  NewsGalleryImage.id.desc())
        .all()
    )


def _get(image_id: int) -> NewsGalleryImage:
    image = database.db_session.get(NewsGalleryImage, image_id)
    if image is None:
        abort(404)
    return image


def _is_ajax() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def index():
    return render_template("news/gallery.html", images=_images())


@bp.route("/picker")
@roles_required(RoleEnum.BOARD)
def picker():
    """Тело окна «Из галереи» в форме новости (подгружается по AJAX)."""
    return render_template("news/_gallery_picker.html", images=_images())


@bp.route("/upload", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def upload():
    """Добавить картинки (можно несколько сразу). Подпись одна на все;
    пусто — имя файла без расширения."""
    title = request.form.get("title", "").strip()[:TITLE_MAX]
    added = 0
    for file_storage in request.files.getlist("images"):
        if not file_storage or not file_storage.filename:
            continue
        stored = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"], allowed_ext=ALLOWED_PHOTO_EXT)
        if not stored:
            continue
        name = file_storage.filename
        database.db_session.add(NewsGalleryImage(
            title=title or os.path.splitext(name)[0][:TITLE_MAX] or _("Картинка"),
            original_filename=name, stored_filename=stored,
            content_type=file_storage.content_type, author_id=g.user.id,
        ))
        added += 1
    if added:
        audit.record("news_gallery.upload", f"В галерею новостей добавлено картинок: {added}"
                     + (f" («{title}»)" if title else ""))
        database.db_session.commit()
    if _is_ajax():
        if not added:
            return jsonify(error=_("Выберите картинку: jpg, png, webp или gif.")), 400
        return jsonify(added=added)
    if added:
        flash(_("Картинки добавлены в галерею: {n}.", n=added), "success")
    else:
        flash(_("Выберите картинку: jpg, png, webp или gif."), "danger")
    return redirect(url_for("news_gallery.index"))


@bp.route("/<int:image_id>/rename", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def rename(image_id):
    image = _get(image_id)
    title = request.form.get("title", "").strip()[:TITLE_MAX]
    if title:
        old = image.title
        image.title = title
        audit.record("news_gallery.rename", f"Картинка галереи новостей «{old}» переименована в «{title}»")
        database.db_session.commit()
        flash(_("Подпись сохранена."), "success")
    return redirect(url_for("news_gallery.index"))


@bp.route("/<int:image_id>/replace", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def replace(image_id):
    """Заменить файл картинки, сохранив подпись и счётчик использования
    (например, обновлённые тарифы). Уже вставленные в новости копии не
    меняются — там отдельные файлы (см. use())."""
    image = _get(image_id)
    file_storage = request.files.get("image")
    folder = current_app.config["UPLOAD_FOLDER"]
    stored = None
    if file_storage and file_storage.filename:
        stored = save_upload(file_storage, folder, allowed_ext=ALLOWED_PHOTO_EXT)
    if not stored:
        flash(_("Выберите картинку: jpg, png, webp или gif."), "danger")
        return redirect(url_for("news_gallery.index"))
    old_stored = image.stored_filename
    image.original_filename = file_storage.filename
    image.stored_filename = stored
    image.content_type = file_storage.content_type
    audit.record("news_gallery.replace", f"В галерее новостей заменена картинка «{image.title}»")
    database.db_session.commit()
    try:
        os.remove(os.path.join(folder, old_stored))
    except OSError:
        pass
    flash(_("Картинка заменена. В уже опубликованных новостях осталась прежняя."), "success")
    return redirect(url_for("news_gallery.index"))


@bp.route("/<int:image_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(image_id):
    image = _get(image_id)
    audit.record("news_gallery.delete", f"Из галереи новостей удалена картинка «{image.title}»")
    database.db_session.delete(image)  # файл убирает after_delete (models._delete_attachment_file)
    database.db_session.commit()
    flash(_("Картинка удалена из галереи. В уже опубликованных новостях она осталась."), "success")
    return redirect(url_for("news_gallery.index"))


@bp.route("/<int:image_id>/image")
@roles_required(RoleEnum.BOARD)
def image(image_id):
    image = _get(image_id)
    return send_file(os.path.join(current_app.config["UPLOAD_FOLDER"], image.stored_filename),
                     download_name=image.original_filename, max_age=86400)


@bp.route("/<int:image_id>/use", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def use(image_id):
    """«Вставить» в окне выбора: копия файла -> «осиротевшее» inline-вложение
    новости (news_id=None, как у news.upload_inline_attachment), его
    забирает сохранение новости. -> {url} для ![](url) в тексте."""
    image = _get(image_id)
    folder = current_app.config["UPLOAD_FOLDER"]
    ext = os.path.splitext(image.stored_filename)[1].lower()
    stored = f"{uuid.uuid4().hex}{ext}"
    try:
        shutil.copyfile(os.path.join(folder, image.stored_filename), os.path.join(folder, stored))
    except OSError:
        return jsonify(error=_("Файл картинки пропал с диска — удалите её из галереи и загрузите заново.")), 410
    att = NewsAttachment(
        news_id=None, original_filename=image.original_filename, stored_filename=stored,
        content_type=image.content_type, is_inline=True, author_id=g.user.id,
    )
    database.db_session.add(att)
    image.use_count = (image.use_count or 0) + 1
    image.last_used_at = dt.datetime.utcnow()
    database.db_session.commit()
    return jsonify(url=url_for("news.attachment", attachment_id=att.id, original_filename=att.original_filename))
