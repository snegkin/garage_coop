"""
Доска объявлений (`/bulletin/`) — общедоступная, как новостная лента: видна
и анонимным посетителям сайта (см. auth.login — та же страница входа), КРОМЕ
объявлений, отмеченных автором как «только для членов кооператива»
(BulletinPost.is_members_only) — те видят только вошедшие пользователи. В
отличие от новостей размещать объявления может любой ВОШЕДШИЙ пользователь,
не только правление; удалить своё объявление может сам автор, любое —
правление (модерация постфактум, без предварительного одобрения перед
публикацией — по прямой просьбе).

Текст объявления — та же упрощённая markdown-разметка, что у новостей и
вики (см. news_format.py), с тем же тулбаром и AJAX-вставкой картинок
прямо в текст (BulletinAttachment, всегда is_inline) — в отличие от
новостей/вики отдельного блока «прикреплённые файлы» здесь нет, для
объявления достаточно фото в тексте.
"""
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_file, jsonify,
)

from . import database
from . import audit
from . import notifications
from .i18n import translate as _, parse_optional_decimal
from .auth import login_required
from .permissions import is_board
from .models import BulletinPost, BulletinAttachment, BulletinCategory
from .uploads import save_upload
from .garages import ALLOWED_PHOTO_EXT
from .news_format import render_html

bp = Blueprint("bulletin", __name__, url_prefix="/bulletin")

# Порядок категорий на форме/фильтре — не алфавитный (Enum) и не как в БД,
# а в порядке, в котором их назвал председатель кооператива.
CATEGORY_LABELS = {
    BulletinCategory.BUY: "Куплю",
    BulletinCategory.SELL: "Продам",
    BulletinCategory.RENT: "Сдам",
    BulletinCategory.LEASE: "Аренда",
    BulletinCategory.SEEKING: "Ищу",
    BulletinCategory.SERVICES: "Услуги",
}
CATEGORY_ORDER = [
    BulletinCategory.BUY, BulletinCategory.SELL, BulletinCategory.RENT,
    BulletinCategory.LEASE, BulletinCategory.SEEKING, BulletinCategory.SERVICES,
]

# Ссылка на вложение внутри markdown-текста объявления — тот же приём, что
# у news.py/wiki.py: распознаём её, чтобы при сохранении «забрать»
# осиротевшие inline-вложения, на которые в тексте появилась ссылка.
INLINE_ATTACHMENT_RE = re.compile(r"/bulletin/attachments/(\d+)/")


def _can_manage(post: BulletinPost) -> bool:
    """Правление — любое объявление (модерация); сам автор — только своё.
    g.user всегда не None здесь — оба вызывающих роута под @login_required."""
    return is_board() or (post.author_id is not None and post.author_id == g.user.id)


def _can_view(post: BulletinPost) -> bool:
    """Общедоступное объявление видит любой; «только для членов» — только
    вошедшие (не обязательно автор/правление — любой вошедший пользователь,
    как и публикация)."""
    return not post.is_members_only or g.user is not None


def _sync_inline_attachments(post: BulletinPost, description_text: str):
    """Приводит is_inline-вложения объявления в соответствие с тем, что
    реально упомянуто в его markdown-тексте, после сохранения
    (create/edit) — тот же приём, что news.py/wiki.py: _sync_inline_attachments."""
    referenced_ids = {int(m) for m in INLINE_ATTACHMENT_RE.findall(description_text)}

    if referenced_ids:
        orphans = database.db_session.query(BulletinAttachment).filter(
            BulletinAttachment.id.in_(referenced_ids),
            BulletinAttachment.post_id.is_(None),
            BulletinAttachment.author_id == g.user.id,
        ).all()
        for att in orphans:
            att.post = post

    for att in list(post.attachments):
        if att.is_inline and att.id not in referenced_ids:
            database.db_session.delete(att)


def latest_posts(limit: int = 5):
    """Последние объявления для виджета на главной (см. app/main.py:
    index()) — те же правила видимости, что и в list_posts(): «только для
    членов» скрыты от анонимных посетителей."""
    query = database.db_session.query(BulletinPost).order_by(BulletinPost.created_at.desc())
    if g.user is None:
        query = query.filter(BulletinPost.is_members_only.is_(False))
    return query.limit(limit).all()


@bp.route("/")
def list_posts():
    category_raw = request.args.get("category")
    query = database.db_session.query(BulletinPost).order_by(BulletinPost.created_at.desc())
    selected_category = None
    if category_raw in BulletinCategory._value2member_map_:
        selected_category = BulletinCategory(category_raw)
        query = query.filter(BulletinPost.category == selected_category)
    if g.user is None:
        query = query.filter(BulletinPost.is_members_only.is_(False))
    posts = query.all()
    return render_template(
        "bulletin/list.html", posts=posts, selected_category=selected_category,
        category_order=CATEGORY_ORDER, category_labels=CATEGORY_LABELS,
    )


@bp.route("/preview", methods=["POST"])
@login_required
def preview():
    """AJAX-предпросмотр markdown из тулбара формы объявления (см.
    bulletin/form.html) — рендерит текущий текст textarea ровно тем же
    render_html(), что и итоговое опубликованное объявление."""
    return jsonify(html=render_html(request.form.get("description", "")))


@bp.route("/attachments/upload", methods=["POST"])
@login_required
def upload_inline_attachment():
    """AJAX-загрузка картинки «на лету» из тулбара формы объявления — ещё
    до того, как само объявление сохранено (см. bulletin/form.html).
    Создаёт «осиротевшее» (post_id=None) is_inline-вложение; окончательно
    привязывается к объявлению при сохранении (см.
    _sync_inline_attachments) — если объявление так и не будет сохранено,
    вложение почистит scripts/cleanup_orphan_attachments.py по cron."""
    file_storage = request.files.get("image")
    if not file_storage or not file_storage.filename:
        return jsonify(error=_("Файл не выбран.")), 400
    stored_name = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"], allowed_ext=ALLOWED_PHOTO_EXT)
    if not stored_name:
        return jsonify(error=_("Недопустимый формат файла. Разрешены: jpg, png, webp, gif.")), 400

    att = BulletinAttachment(
        post_id=None,
        original_filename=file_storage.filename,
        stored_filename=stored_name,
        content_type=file_storage.content_type,
        is_inline=True,
        author_id=g.user.id,
    )
    database.db_session.add(att)
    database.db_session.commit()
    return jsonify(url=url_for("bulletin.attachment", attachment_id=att.id, original_filename=att.original_filename))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "GET":
        return render_template("bulletin/form.html", post=None, category_order=CATEGORY_ORDER, category_labels=CATEGORY_LABELS)

    f = request.form
    category_raw = f.get("category")
    title = f.get("title", "").strip()
    description = f.get("description", "").strip()
    contact = f.get("contact", "").strip()
    if category_raw not in BulletinCategory._value2member_map_ or not title or not description or not contact:
        flash(_("Заполните вид объявления, заголовок, текст и контакт для связи."), "danger")
        return redirect(url_for("bulletin.create"))
    # Некорректная цена (не разбирается как число) молча становится "цена
    # не указана" — тот же принцип, что и везде в проекте у необязательных
    # денежных полей через parse_optional_decimal (см. app/i18n.py) — не
    # блокирует публикацию объявления ради опечатки в необязательном поле.
    price = parse_optional_decimal(f.get("price"))

    post = BulletinPost(
        category=BulletinCategory(category_raw), title=title, description=description,
        price=price, contact=contact, is_members_only=bool(f.get("is_members_only")),
        author_id=g.user.id,
    )
    database.db_session.add(post)
    database.db_session.flush()
    _sync_inline_attachments(post, description)
    audit.record(
        "bulletin.create", entity_type="bulletin_post", entity_id=post.id,
        summary=f"Добавлено объявление «{title}» ({CATEGORY_LABELS[post.category]})",
    )
    database.db_session.commit()
    notifications.notify_subscribers(
        "news", "Новое объявление на доске", title, exclude_user_id=g.user.id,
    )
    flash(_("Объявление опубликовано."), "success")
    return redirect(url_for("bulletin.list_posts"))


@bp.route("/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def edit(post_id):
    post = database.db_session.get(BulletinPost, post_id)
    if post is None:
        abort(404)
    if not _can_manage(post):
        abort(403)

    if request.method == "GET":
        return render_template("bulletin/form.html", post=post, category_order=CATEGORY_ORDER, category_labels=CATEGORY_LABELS)

    f = request.form
    category_raw = f.get("category")
    title = f.get("title", "").strip()
    description = f.get("description", "").strip()
    contact = f.get("contact", "").strip()
    if category_raw not in BulletinCategory._value2member_map_ or not title or not description or not contact:
        flash(_("Заполните вид объявления, заголовок, текст и контакт для связи."), "danger")
        return redirect(url_for("bulletin.edit", post_id=post.id))
    price = parse_optional_decimal(f.get("price"))

    post.category = BulletinCategory(category_raw)
    post.title = title
    post.description = description
    post.contact = contact
    post.price = price
    post.is_members_only = bool(f.get("is_members_only"))

    _sync_inline_attachments(post, description)
    audit.record(
        "bulletin.edit", entity_type="bulletin_post", entity_id=post.id,
        summary=f"Изменено объявление «{title}»",
    )
    database.db_session.commit()
    flash(_("Объявление сохранено."), "success")
    return redirect(url_for("bulletin.list_posts"))


@bp.route("/<int:post_id>/delete", methods=["POST"])
@login_required
def delete(post_id):
    post = database.db_session.get(BulletinPost, post_id)
    if post is None:
        abort(404)
    if not _can_manage(post):
        abort(403)

    title = post.title
    database.db_session.delete(post)
    audit.record(
        "bulletin.delete", entity_type="bulletin_post", entity_id=post_id,
        summary=f"Удалено объявление «{title}»",
    )
    database.db_session.commit()
    flash(_("Объявление удалено."), "success")
    return redirect(url_for("bulletin.list_posts"))


@bp.route("/attachments/<int:attachment_id>/<path:original_filename>")
def attachment(attachment_id, original_filename):
    """Отдаёт файл/фото вложения. Путь включает исходное имя файла только
    для красивого URL и правильного имени при скачивании — файл на диске
    ищем по attachment_id. Наследует видимость от самого объявления
    (is_members_only) — иначе вложение «только для членов» было бы
    доступно по прямой ссылке в обход ограничения; «осиротевшее» (ещё не
    привязанное к объявлению, только что загруженное в открытой форме)
    вложение отдаётся любому вошедшему — создать его мог только вошедший
    пользователь через upload_inline_attachment."""
    att = database.db_session.get(BulletinAttachment, attachment_id)
    if att is None:
        abort(404)
    if att.post is not None and not _can_view(att.post):
        abort(403)
    if att.post is None and g.user is None:
        abort(403)
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    return send_file(
        os.path.join(upload_folder, att.stored_filename),
        as_attachment=True,
        download_name=att.original_filename,
    )
