import datetime as dt

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_from_directory,
)

from . import database
from .i18n import translate as _
from .auth import roles_required
from .models import News, NewsAttachment, RoleEnum
from .uploads import save_upload
from .news_format import render_html, excerpt

bp = Blueprint("news", __name__, url_prefix="/news")

# Сколько последних новостей показывать на главной (странице входа) —
# лента там не для архива, а для актуальных объявлений правления.
FRONT_PAGE_LIMIT = 5


def latest_news(limit: int = FRONT_PAGE_LIMIT):
    """Последние новости для отображения на главной. Отдельная функция,
    т.к. вызывается и из news.py (админка), и из auth.py (страница входа,
    доступна анонимным посетителям — там нет своего блюпринта для этого)."""
    return database.db_session.query(News).order_by(News.created_at.desc()).limit(limit).all()


def _save_attachments(item: News):
    """Сохраняет все файлы из request.files['attachments'] (multiple) и
    привязывает их к новости. Без ограничения по расширению — как и у
    остальных загрузок в проекте (документы, протоколы и т.д.)."""
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
        ))


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
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"], att.stored_filename,
        download_name=att.original_filename,
    )
