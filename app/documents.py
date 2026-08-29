import datetime as dt
import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_file, abort
from werkzeug.utils import secure_filename

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required, is_safe_next_url
from .permissions import is_board
from .models import Document, DocumentType, RoleEnum
from .uploads import save_upload

bp = Blueprint("documents", __name__, url_prefix="/documents")


@bp.route("/")
@login_required
def list_documents():
    doc_type = request.args.get("type")
    query = database.db_session.query(Document)
    if doc_type:
        query = query.filter(Document.doc_type == DocumentType(doc_type))
    if not is_board():
        # Внутренние документы (is_internal) видны только правлению — рядовой
        # член кооператива их вообще не видит в списке (не просто без ссылки
        # на файл, а полностью, включая номер/название) и не сможет скачать
        # напрямую по id, см. проверку в download() ниже.
        query = query.filter(Document.is_internal.is_(False))
    docs = query.order_by(Document.date.desc()).all()
    return render_template("documents/list.html", docs=docs, doc_types=DocumentType, selected_type=doc_type)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        file_storage = request.files.get("file")
        file_path = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"])

        doc = Document(
            doc_type=DocumentType(f["doc_type"]),
            number=f.get("number") or None,
            date=dt.date.fromisoformat(f["date"]),
            title=f["title"],
            file_path=file_path,
            file_name=secure_filename(file_storage.filename) if file_storage and file_storage.filename else None,
            comment=f.get("comment") or None,
            is_internal=bool(f.get("is_internal")),
        )
        database.db_session.add(doc)
        database.db_session.commit()
        flash(_("Документ добавлен."), "success")

        next_url = request.form.get("next")
        if is_safe_next_url(next_url):
            return redirect(next_url)
        return redirect(url_for("documents.list_documents"))

    return render_template("documents/form.html", doc_types=DocumentType)


@bp.route("/<int:doc_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None:
        flash(_("Документ не найден."), "danger")
        return redirect(url_for("documents.list_documents"))

    f = request.form
    doc.doc_type = DocumentType(f["doc_type"])
    doc.number = f.get("number") or None
    doc.date = dt.date.fromisoformat(f["date"])
    doc.title = f["title"]
    doc.comment = f.get("comment") or None
    doc.is_internal = bool(f.get("is_internal"))

    file = request.files.get("file")
    if file and file.filename:
        doc.file_path = save_upload(file, current_app.config["UPLOAD_FOLDER"])
        doc.file_name = secure_filename(file.filename)

    database.db_session.commit()
    flash(_("Документ обновлён."), "success")

    next_url = request.form.get("next")
    if is_safe_next_url(next_url):
        return redirect(next_url)
    return redirect(url_for("documents.list_documents"))


@bp.route("/<int:doc_id>/file")
@login_required
def download(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None or not doc.file_path:
        flash(_("Файл не найден."), "danger")
        return redirect(url_for("documents.list_documents"))
    if doc.is_internal and not is_board():
        abort(403)
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    stored_name = doc.file_path
    original_name = doc.file_name or stored_name
    return send_file(
        os.path.join(upload_folder, stored_name),
        as_attachment=True,
        download_name=original_name,
    )


@bp.route("/<int:doc_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None:
        flash(_("Документ не найден."), "danger")
        return redirect(url_for("documents.list_documents"))

    # Удаляем физический файл с диска
    if doc.file_path:
        upload_folder = current_app.config["UPLOAD_FOLDER"]
        file_path = os.path.join(upload_folder, doc.file_path)
        if os.path.exists(file_path):
            os.remove(file_path)

    database.db_session.delete(doc)
    database.db_session.commit()
    flash(_("Документ удалён."), "success")
    return redirect(url_for("documents.list_documents"))
