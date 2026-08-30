"""Вики кооператива: справочные заметки (параметры видеонаблюдения,
структура сети, телефоны контрагентов и аварийных служб и т.п.) —
в отличие от News (хронологическая лента объявлений), это набор страниц,
которые правятся по мере необходимости. Формат тела — тот же упрощённый
markdown, что и у новостей (см. app/news_format.py, переиспользуется как
есть — модуль не завязан на модель News).

Создают/редактируют/удаляют только правление (RoleEnum.BOARD), как и
документы. Видимость страницы для ЧТЕНИЯ настраивается персонально через
WikiPage.is_internal — тем же принципом, что и Document.is_internal (см.
app/documents.py): общедоступная страница видна любому вошедшему члену,
внутренняя — только правлению, включая прямой переход по id (без 403 в
списке недостаточно, см. download() в documents.py — тот же приём здесь).
"""
import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .permissions import is_board
from .models import WikiPage, RoleEnum

bp = Blueprint("wiki", __name__, url_prefix="/wiki")


@bp.route("/")
@login_required
def list_pages():
    category = request.args.get("category")
    query = database.db_session.query(WikiPage)
    if category:
        query = query.filter(WikiPage.category == category)
    if not is_board():
        query = query.filter(WikiPage.is_internal.is_(False))
    pages = query.order_by(WikiPage.title.asc()).all()
    return render_template("wiki/list.html", pages=pages, categories=_existing_categories(), selected_category=category)


def _existing_categories():
    """Список уже использованных категорий — для фильтра в списке и для
    datalist-подсказки в форме (свободный текст, не enum, см. WikiPage)."""
    return [
        row[0] for row in database.db_session.query(WikiPage.category)
        .filter(WikiPage.category.isnot(None))
        .distinct().order_by(WikiPage.category.asc()).all()
    ]


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        page = WikiPage(
            title=f["title"],
            category=f.get("category") or None,
            body=f["body"],
            is_internal=bool(f.get("is_internal")),
            author_id=g.user.id,
        )
        database.db_session.add(page)
        database.db_session.commit()
        flash(_("Страница вики добавлена."), "success")
        return redirect(url_for("wiki.view", page_id=page.id))

    return render_template("wiki/form.html", page=None, categories=_existing_categories())


@bp.route("/<int:page_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(page_id):
    page = database.db_session.get(WikiPage, page_id)
    if page is None:
        abort(404)

    if request.method == "POST":
        f = request.form
        page.title = f["title"]
        page.category = f.get("category") or None
        page.body = f["body"]
        page.is_internal = bool(f.get("is_internal"))
        page.updated_at = dt.datetime.utcnow()
        page.updated_by_id = g.user.id
        database.db_session.commit()
        flash(_("Страница вики обновлена."), "success")
        return redirect(url_for("wiki.view", page_id=page.id))

    return render_template("wiki/form.html", page=page, categories=_existing_categories())


@bp.route("/<int:page_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(page_id):
    page = database.db_session.get(WikiPage, page_id)
    if page is None:
        abort(404)
    database.db_session.delete(page)
    database.db_session.commit()
    flash(_("Страница вики удалена."), "success")
    return redirect(url_for("wiki.list_pages"))


@bp.route("/<int:page_id>")
@login_required
def view(page_id):
    page = database.db_session.get(WikiPage, page_id)
    if page is None:
        abort(404)
    if page.is_internal and not is_board():
        abort(403)
    return render_template("wiki/view.html", page=page)
