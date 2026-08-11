import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_from_directory

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
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
    docs = query.order_by(Document.date.desc()).all()
    return render_template("documents/list.html", docs=docs, doc_types=DocumentType, selected_type=doc_type)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        file_path = save_upload(request.files.get("file"), current_app.config["UPLOAD_FOLDER"])

        doc = Document(
            doc_type=DocumentType(f["doc_type"]),
            number=f.get("number") or None,
            date=dt.date.fromisoformat(f["date"]),
            title=f["title"],
            file_path=file_path,
            comment=f.get("comment") or None,
        )
        database.db_session.add(doc)
        database.db_session.commit()
        flash(_("Документ добавлен."), "success")

        next_url = request.form.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("documents.list_documents"))

    return render_template("documents/form.html", doc_types=DocumentType)


@bp.route("/<int:doc_id>/file")
@login_required
def download(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None or not doc.file_path:
        flash(_("Файл не найден."), "danger")
        return redirect(url_for("documents.list_documents"))
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], doc.file_path)
