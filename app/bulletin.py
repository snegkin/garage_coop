"""
Доска объявлений (`/bulletin/`) — общедоступная, как новостная лента: видна
и анонимным посетителям сайта (см. auth.login — та же страница входа).
В отличие от новостей размещать объявления может любой ВОШЕДШИЙ
пользователь, не только правление; удалить своё объявление может сам
автор, любое — правление (модерация постфактум, без предварительного
одобрения перед публикацией — по прямой просьбе).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

from . import database
from . import audit
from .i18n import translate as _, parse_optional_decimal
from .auth import login_required
from .permissions import is_board
from .models import BulletinPost, BulletinCategory

bp = Blueprint("bulletin", __name__, url_prefix="/bulletin")

# Порядок категорий на форме/фильтре — не алфавитный (Enum) и не как в БД,
# а в порядке, в котором их назвал председатель кооператива.
CATEGORY_LABELS = {
    BulletinCategory.BUY: "Куплю",
    BulletinCategory.SELL: "Продам",
    BulletinCategory.RENT: "Сдам",
    BulletinCategory.SERVICES: "Услуги",
}
CATEGORY_ORDER = [BulletinCategory.BUY, BulletinCategory.SELL, BulletinCategory.RENT, BulletinCategory.SERVICES]


def _can_manage(post: BulletinPost) -> bool:
    """Правление — любое объявление (модерация); сам автор — только своё.
    g.user всегда не None здесь — оба вызывающих роута под @login_required."""
    return is_board() or (post.author_id is not None and post.author_id == g.user.id)


@bp.route("/")
def list_posts():
    category_raw = request.args.get("category")
    query = database.db_session.query(BulletinPost).order_by(BulletinPost.created_at.desc())
    selected_category = None
    if category_raw in BulletinCategory._value2member_map_:
        selected_category = BulletinCategory(category_raw)
        query = query.filter(BulletinPost.category == selected_category)
    posts = query.all()
    return render_template(
        "bulletin/list.html", posts=posts, selected_category=selected_category,
        category_order=CATEGORY_ORDER, category_labels=CATEGORY_LABELS,
    )


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
        price=price, contact=contact, author_id=g.user.id,
    )
    database.db_session.add(post)
    database.db_session.flush()
    audit.record(
        "bulletin.create", entity_type="bulletin_post", entity_id=post.id,
        summary=f"Добавлено объявление «{title}» ({CATEGORY_LABELS[post.category]})",
    )
    database.db_session.commit()
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
