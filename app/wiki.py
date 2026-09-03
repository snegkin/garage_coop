"""Вики кооператива: справочные заметки (параметры видеонаблюдения,
структура сети, телефоны контрагентов и аварийных служб и т.п.) —
в отличие от News (хронологическая лента объявлений), это набор страниц,
организованных ДЕРЕВОМ разделов/подразделов (WikiPage.parent_id,
самоссылающийся FK, глубина не ограничена), которые правятся по мере
необходимости. Формат тела — тот же упрощённый markdown, что и у новостей
(см. app/news_format.py, переиспользуется как есть — модуль не завязан на
модель News).

Создают/редактируют/удаляют только правление (RoleEnum.BOARD), как и
документы — раздел ничем не отличается от обычной страницы, кроме
наличия детей (см. _build_visible_tree). Видимость страницы для ЧТЕНИЯ
настраивается персонально через WikiPage.is_internal — тем же принципом,
что и Document.is_internal (см. app/cooperative.documents): общедоступная страница
видна любому вошедшему члену, внутренняя — только правлению, включая
прямой переход по id (без 403 в списке недостаточно, см. download() в
cooperative.documents — тот же приём здесь).
"""
import datetime as dt
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_file, jsonify,
)

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .permissions import is_board
from .models import WikiPage, WikiAttachment, RoleEnum
from .uploads import save_upload
from .garages import ALLOWED_PHOTO_EXT
from .news_format import render_html

bp = Blueprint("wiki", __name__, url_prefix="/wiki")

# см. news.py: INLINE_ATTACHMENT_RE — тот же приём, свой префикс пути.
INLINE_ATTACHMENT_RE = re.compile(r"/wiki/attachments/(\d+)/")


# ---------------------------------------------------------------------------
# Дерево страниц
# ---------------------------------------------------------------------------

def _build_visible_tree(all_pages, viewer_is_board):
    """Строит дерево для навигации из ПОЛНОГО списка страниц с учётом
    видимости: скрытые от текущего пользователя узлы (is_internal при
    просмотре не-правлением) исключаются, а их видимые потомки поднимаются
    к ближайшему видимому предку (или в корень) — чтобы в дереве не было
    «дыр» (видимая подстраница не пропадает только из-за того, что раздел
    над ней сделали внутренним). Каждый узел — dict {"page": WikiPage,
    "children": [...]}, отсортировано по алфавиту на каждом уровне."""
    by_id = {p.id: p for p in all_pages}

    def is_visible(p):
        return viewer_is_board or not p.is_internal

    def nearest_visible_ancestor_id(p):
        cur = by_id.get(p.parent_id)
        while cur is not None and not is_visible(cur):
            cur = by_id.get(cur.parent_id)
        return cur.id if cur is not None else None

    nodes = {p.id: {"page": p, "children": []} for p in all_pages if is_visible(p)}

    roots = []
    for p in all_pages:
        if p.id not in nodes:
            continue
        eff_parent_id = p.parent_id
        if eff_parent_id is not None and eff_parent_id not in nodes:
            eff_parent_id = nearest_visible_ancestor_id(p)
        if eff_parent_id is None:
            roots.append(nodes[p.id])
        else:
            nodes[eff_parent_id]["children"].append(nodes[p.id])

    def sort_rec(items):
        items.sort(key=lambda n: n["page"].title.lower())
        for it in items:
            sort_rec(it["children"])

    sort_rec(roots)
    return roots


def _ancestor_ids(page):
    """id всех предков страницы (для авто-раскрытия её ветки в дереве при
    просмотре и для хлебных крошек)."""
    ids = set()
    cur = page.parent
    while cur is not None:
        ids.add(cur.id)
        cur = cur.parent
    return ids


def _descendant_ids(page):
    """id страницы и всех её потомков (рекурсивно) — чтобы при выборе
    родителя в форме редактирования нельзя было выбрать саму страницу или
    её же потомка (иначе дерево зациклится)."""
    ids = {page.id}
    stack = list(page.children)
    while stack:
        node = stack.pop()
        ids.add(node.id)
        stack.extend(node.children)
    return ids


def _parent_options(all_pages, exclude_ids=frozenset()):
    """Список (page, depth) для выпадающего списка «Родительская
    страница» в форме — с отступом по глубине для читаемости, в том же
    порядке, что и дерево навигации."""
    tree = _build_visible_tree(all_pages, viewer_is_board=True)  # форма — только для правления, видно всё дерево целиком

    options = []

    def walk(nodes, depth):
        for node in nodes:
            if node["page"].id not in exclude_ids:
                options.append((node["page"], depth))
            walk(node["children"], depth + 1)

    walk(tree, 0)
    return options


# ---------------------------------------------------------------------------
# Inline-вложения (картинки в теле страницы)
# ---------------------------------------------------------------------------

def _sync_inline_attachments(page: WikiPage, body_text: str):
    """См. news.py: _sync_inline_attachments — тот же приём для страницы
    вики: забирает свои осиротевшие вложения, на которые появилась ссылка
    в новом теле, удаляет прикреплённые, ссылку на которые убрали."""
    referenced_ids = {int(m) for m in INLINE_ATTACHMENT_RE.findall(body_text)}

    if referenced_ids:
        orphans = database.db_session.query(WikiAttachment).filter(
            WikiAttachment.id.in_(referenced_ids),
            WikiAttachment.page_id.is_(None),
            WikiAttachment.author_id == g.user.id,
        ).all()
        for att in orphans:
            att.page = page

    for att in list(page.attachments):
        if att.id not in referenced_ids:
            database.db_session.delete(att)


@bp.route("/preview", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def preview():
    """AJAX-предпросмотр markdown из тулбара формы страницы вики — см.
    news.py: preview, тот же приём (тот же render_html, что и у новостей —
    вики использует тот же markdown-рендер)."""
    return jsonify(html=render_html(request.form.get("body", "")))


@bp.route("/attachments/upload", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def upload_inline_attachment():
    """AJAX-загрузка картинки «на лету» из тулбара формы страницы вики —
    см. news.py: upload_inline_attachment, тот же приём."""
    file_storage = request.files.get("image")
    if not file_storage or not file_storage.filename:
        return jsonify(error=_("Файл не выбран.")), 400
    stored_name = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"], allowed_ext=ALLOWED_PHOTO_EXT)
    if not stored_name:
        return jsonify(error=_("Недопустимый формат файла. Разрешены: jpg, png, webp, gif.")), 400

    att = WikiAttachment(
        page_id=None,
        original_filename=file_storage.filename,
        stored_filename=stored_name,
        content_type=file_storage.content_type,
        author_id=g.user.id,
    )
    database.db_session.add(att)
    database.db_session.commit()
    return jsonify(url=url_for("wiki.attachment", attachment_id=att.id, original_filename=att.original_filename))


@bp.route("/attachments/<int:attachment_id>/<path:original_filename>")
@login_required
def attachment(attachment_id, original_filename):
    """Отдаёт картинку, вставленную в тело страницы вики. В отличие от
    новостей (полностью публичная лента) вики видна только вошедшим
    пользователям — и видимость картинки наследует видимость страницы:
    если страница is_internal, файл доступен только правлению, даже по
    прямой ссылке (та же логика, что и у самой страницы, см. view()).
    Для «осиротевшего» (page is None, статья/правка ещё не сохранены)
    вложения по умолчанию считаем его внутренним — доступ только
    правлению, т.к. отдать «неизвестно что» анонимно/рядовому члену не
    должны."""
    att = database.db_session.get(WikiAttachment, attachment_id)
    if att is None:
        abort(404)
    if att.page is None or att.page.is_internal:
        if not is_board():
            abort(403)
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    return send_file(
        os.path.join(upload_folder, att.stored_filename),
        as_attachment=True,
        download_name=att.original_filename,
    )


# ---------------------------------------------------------------------------
# CRUD страниц
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def list_pages():
    all_pages = database.db_session.query(WikiPage).all()
    tree = _build_visible_tree(all_pages, viewer_is_board=is_board())
    return render_template("wiki/list.html", tree=tree)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        parent_id = int(f["parent_id"]) if f.get("parent_id") else None
        page = WikiPage(
            title=f["title"],
            parent_id=parent_id,
            body=f["body"],
            is_internal=bool(f.get("is_internal")),
            author_id=g.user.id,
        )
        database.db_session.add(page)
        _sync_inline_attachments(page, f["body"])
        database.db_session.commit()
        flash(_("Страница вики добавлена."), "success")
        return redirect(url_for("wiki.view", page_id=page.id))

    all_pages = database.db_session.query(WikiPage).all()
    preselected_parent_id = request.args.get("parent_id", type=int)
    return render_template("wiki/form.html", page=None, parent_options=_parent_options(all_pages),
                            preselected_parent_id=preselected_parent_id)


@bp.route("/<int:page_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(page_id):
    page = database.db_session.get(WikiPage, page_id)
    if page is None:
        abort(404)

    if request.method == "POST":
        f = request.form
        parent_id = int(f["parent_id"]) if f.get("parent_id") else None
        if parent_id is not None and parent_id in _descendant_ids(page):
            flash(_("Нельзя сделать родителем саму страницу или её же подраздел."), "danger")
            all_pages = database.db_session.query(WikiPage).all()
            return render_template("wiki/form.html", page=page,
                                    parent_options=_parent_options(all_pages, exclude_ids=_descendant_ids(page)),
                                    preselected_parent_id=None)

        page.title = f["title"]
        page.parent_id = parent_id
        page.body = f["body"]
        page.is_internal = bool(f.get("is_internal"))
        page.updated_at = dt.datetime.utcnow()
        page.updated_by_id = g.user.id
        _sync_inline_attachments(page, f["body"])
        database.db_session.commit()
        flash(_("Страница вики обновлена."), "success")
        return redirect(url_for("wiki.view", page_id=page.id))

    all_pages = database.db_session.query(WikiPage).all()
    return render_template("wiki/form.html", page=page,
                            parent_options=_parent_options(all_pages, exclude_ids=_descendant_ids(page)),
                            preselected_parent_id=None)


@bp.route("/<int:page_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(page_id):
    page = database.db_session.get(WikiPage, page_id)
    if page is None:
        abort(404)
    if page.children:
        flash(_("Нельзя удалить раздел, в котором есть подразделы/страницы — сначала удалите или перенесите их."), "danger")
        return redirect(url_for("wiki.view", page_id=page.id))
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

    all_pages = database.db_session.query(WikiPage).all()
    tree = _build_visible_tree(all_pages, viewer_is_board=is_board())
    path_ids = _ancestor_ids(page) | {page.id}

    breadcrumbs = []
    cur = page.parent
    while cur is not None:
        breadcrumbs.append(cur)
        cur = cur.parent
    breadcrumbs.reverse()

    return render_template("wiki/view.html", page=page, tree=tree, path_ids=path_ids, breadcrumbs=breadcrumbs)
