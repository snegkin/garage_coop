import datetime as dt
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_file, jsonify,
)

from . import database
from .i18n import translate as _
from .auth import roles_required
from .models import News, NewsAttachment, RoleEnum
from .uploads import save_upload
from .garages import ALLOWED_PHOTO_EXT
from .news_format import render_html, excerpt

bp = Blueprint("news", __name__, url_prefix="/news")

# Сколько последних новостей показывать на главной (странице входа) —
# лента там не для архива, а для актуальных объявлений правления.
FRONT_PAGE_LIMIT = 5

# Ссылка на вложение внутри markdown-текста статьи — распознаём её, чтобы
# при сохранении «забрать» осиротевшие inline-вложения, на которые в тексте
# появилась ссылка (см. _sync_inline_attachments). Path, не полный URL —
# работает независимо от домена/схемы, есть в любой сгенерированной
# url_for('news.attachment', ...) ссылке.
INLINE_ATTACHMENT_RE = re.compile(r"/news/attachments/(\d+)/")


def latest_news(limit: int = FRONT_PAGE_LIMIT):
    """Последние новости для отображения на главной. Отдельная функция,
    т.к. вызывается и из news.py (админка), и из auth.py (страница входа,
    доступна анонимным посетителям — там нет своего блюпринта для этого)."""
    return database.db_session.query(News).order_by(News.created_at.desc()).limit(limit).all()


def _save_attachments(item: News):
    """Сохраняет все файлы из request.files['attachments'] (multiple) и
    привязывает их к новости. save_upload() применяет белый список
    расширений по умолчанию (DEFAULT_ALLOWED_EXT) — этот эндпоинт публичный
    (вложения новостей отдаются анонимным посетителям), поэтому важно не
    дать залить .html/.svg и получить stored XSS."""
    for file_storage in request.files.getlist("attachments"):
        if not file_storage or not file_storage.filename:
            continue
        stored_name = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"])
        if not stored_name:
            continue
        database.db_session.add(NewsAttachment(
            news=item,
            original_filename=file_storage.filename,
            stored_filename=stored_name,
            content_type=file_storage.content_type,
            is_inline=False,
            author_id=g.user.id,
        ))


def _sync_inline_attachments(item: News, body_text: str):
    """Приводит is_inline-вложения статьи в соответствие с тем, что реально
    упомянуто в её markdown-теле, после сохранения (create/edit):
    - «забирает» (news_id=None -> item) свои же осиротевшие inline-вложения,
      на которые в НОВОМ тексте появилась ссылка;
    - удаляет уже прикреплённые inline-вложения, ссылку на которые из
      текста убрали (для галерейных is_inline=False ничего не трогает —
      те управляются чекбоксами remove_attachment, отдельная логика)."""
    referenced_ids = {int(m) for m in INLINE_ATTACHMENT_RE.findall(body_text)}

    if referenced_ids:
        orphans = database.db_session.query(NewsAttachment).filter(
            NewsAttachment.id.in_(referenced_ids),
            NewsAttachment.news_id.is_(None),
            NewsAttachment.author_id == g.user.id,
        ).all()
        for att in orphans:
            att.news = item

    for att in list(item.attachments):
        if att.is_inline and att.id not in referenced_ids:
            database.db_session.delete(att)


@bp.route("/attachments/upload", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def upload_inline_attachment():
    """AJAX-загрузка картинки «на лету» из тулбара формы новости — ещё до
    того, как сама статья сохранена (см. news/form.html). Создаёт
    «осиротевшее» (news_id=None) is_inline-вложение; окончательно
    привязывается к статье при сохранении (см. _sync_inline_attachments) —
    если статья так и не будет сохранена, вложение почистит
    scripts/cleanup_orphan_attachments.py по cron."""
    file_storage = request.files.get("image")
    if not file_storage or not file_storage.filename:
        return jsonify(error=_("Файл не выбран.")), 400
    stored_name = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"], allowed_ext=ALLOWED_PHOTO_EXT)
    if not stored_name:
        return jsonify(error=_("Недопустимый формат файла. Разрешены: jpg, png, webp, gif.")), 400

    att = NewsAttachment(
        news_id=None,
        original_filename=file_storage.filename,
        stored_filename=stored_name,
        content_type=file_storage.content_type,
        is_inline=True,
        author_id=g.user.id,
    )
    database.db_session.add(att)
    database.db_session.commit()
    return jsonify(url=url_for("news.attachment", attachment_id=att.id, original_filename=att.original_filename))


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_news():
    items = database.db_session.query(News).order_by(News.created_at.desc()).all()
    return render_template("news/list.html", items=items)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        item = News(
            title=f["title"],
            body=f["body"],
            author_id=g.user.id,
        )
        database.db_session.add(item)
        _sync_inline_attachments(item, f["body"])
        _save_attachments(item)
        database.db_session.commit()
        flash(_("Новость добавлена."), "success")
        return redirect(url_for("news.list_news"))

    return render_template("news/form.html", item=None)


@bp.route("/<int:news_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(news_id):
    item = database.db_session.get(News, news_id)
    if item is None:
        abort(404)

    if request.method == "POST":
        f = request.form
        item.title = f["title"]
        item.body = f["body"]
        item.updated_at = dt.datetime.utcnow()

        remove_ids = {int(x) for x in request.form.getlist("remove_attachment")}
        if remove_ids:
            for att in list(item.attachments):
                if att.id in remove_ids:
                    database.db_session.delete(att)

        _sync_inline_attachments(item, f["body"])
        _save_attachments(item)
        database.db_session.commit()
        flash(_("Новость обновлена."), "success")
        return redirect(url_for("news.list_news"))

    return render_template("news/form.html", item=item)


@bp.route("/<int:news_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(news_id):
    item = database.db_session.get(News, news_id)
    if item is None:
        abort(404)
    database.db_session.delete(item)
    database.db_session.commit()
    flash(_("Новость удалена."), "success")
    return redirect(url_for("news.list_news"))


@bp.route("/<int:news_id>")
def view(news_id):
    """Полная новость. Публичная страница (как и /auth/login, куда ведёт
    ссылка "Читать дальше") — доступна и анонимным посетителям."""
    item = database.db_session.get(News, news_id)
    if item is None:
        abort(404)
    return render_template("news/view.html", item=item)


@bp.route("/attachments/<int:attachment_id>/<path:original_filename>")
def attachment(attachment_id, original_filename):
    """Отдаёт файл/фото вложения. Путь включает исходное имя файла только
    для красивого URL и правильного имени при скачивании — файл на диске
    ищем по attachment_id."""
    att = database.db_session.get(NewsAttachment, attachment_id)
    if att is None:
        abort(404)
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    return send_file(
        os.path.join(upload_folder, att.stored_filename),
        as_attachment=True,
        download_name=att.original_filename,
    )
