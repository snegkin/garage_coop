"""
Форум (`/forum/`) — свободное обсуждение любых вопросов вошедшими членами
кооператива, не привязанное к конкретному гаражу/собранию/другой
сущности (по прямой просьбе). В отличие от доски объявлений (bulletin.py)
форум НЕ общедоступен — все роуты под @login_required, анонимный
посетитель сюда не попадает вовсе. Категорий/разделов нет — один общий
список тем (по прямой просьбе, проще для старта).

Тема = ForumTopic + первое ForumPost, созданные вместе (create()); ответы
— тоже ForumPost с тем же topic_id. Текст — та же упрощённая markdown-
разметка, что у новостей/вики/доски объявлений (см. news_format.py), с
тем же тулбаром и AJAX-вставкой картинок прямо в текст (ForumAttachment,
всегда is_inline) — общий редактор вынесен в forum/_editor.html, чтобы не
дублировать JS между формой новой темы, формой ответа и формой правки.

Завести тему/ответить может любой вошедший; редактировать — только автор
своего сообщения (правление НЕ правит чужой текст); удалять свою тему —
автор, любую — правление (модерация постфактум, тот же принцип, что и у
доски объявлений); закрывать тему для новых ответов — только правление.
"""
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_file, jsonify,
)

from . import database
from . import audit
from .i18n import translate as _
from .auth import login_required, roles_required
from .permissions import is_board
from .models import ForumTopic, ForumPost, ForumAttachment, RoleEnum
from .uploads import save_upload
from .garages import ALLOWED_PHOTO_EXT
from .news_format import render_html

bp = Blueprint("forum", __name__, url_prefix="/forum")

# Ссылка на вложение внутри markdown-текста сообщения — тот же приём, что
# у bulletin.py/news.py/wiki.py: распознаём её, чтобы при сохранении
# «забрать» осиротевшие inline-вложения, на которые в тексте появилась ссылка.
INLINE_ATTACHMENT_RE = re.compile(r"/forum/attachments/(\d+)/")


def _can_manage_topic(topic: ForumTopic) -> bool:
    """Правление — любая тема (модерация); сам автор — только своя.
    g.user всегда не None здесь — оба вызывающих роута под @login_required."""
    return is_board() or (topic.author_id is not None and topic.author_id == g.user.id)


def _can_manage_post(post: ForumPost) -> bool:
    """Удалить сообщение может правление или сам автор (см. модуль —
    правка текста наоборот доступна ТОЛЬКО автору, см. _can_edit_post)."""
    return is_board() or (post.author_id is not None and post.author_id == g.user.id)


def _can_edit_post(post: ForumPost) -> bool:
    """Править текст сообщения может только сам автор — в отличие от
    удаления, правление сюда не допущено (не переписывает чужие слова)."""
    return post.author_id is not None and post.author_id == g.user.id


def _is_first_post(post: ForumPost) -> bool:
    """Первое (открывающее тему) сообщение удаляется только вместе с
    темой целиком — иначе тема осталась бы без начала."""
    return bool(post.topic.posts) and post.topic.posts[0].id == post.id


def _sync_inline_attachments(post: ForumPost, body_text: str):
    """Приводит is_inline-вложения сообщения в соответствие с тем, что
    реально упомянуто в его markdown-тексте — тот же приём, что в
    bulletin.py/news.py/wiki.py."""
    referenced_ids = {int(m) for m in INLINE_ATTACHMENT_RE.findall(body_text)}

    if referenced_ids:
        orphans = database.db_session.query(ForumAttachment).filter(
            ForumAttachment.id.in_(referenced_ids),
            ForumAttachment.post_id.is_(None),
            ForumAttachment.author_id == g.user.id,
        ).all()
        for att in orphans:
            att.post = post

    for att in list(post.attachments):
        if att.is_inline and att.id not in referenced_ids:
            database.db_session.delete(att)


@bp.route("/")
@login_required
def list_topics():
    topics = database.db_session.query(ForumTopic).order_by(ForumTopic.last_activity_at.desc()).all()
    return render_template("forum/list.html", topics=topics)


@bp.route("/preview", methods=["POST"])
@login_required
def preview():
    """AJAX-предпросмотр markdown из тулбара редактора (см.
    forum/_editor.html) — рендерит текущий текст textarea ровно тем же
    render_html(), что и опубликованное сообщение."""
    return jsonify(html=render_html(request.form.get("body", "")))


@bp.route("/attachments/upload", methods=["POST"])
@login_required
def upload_inline_attachment():
    """AJAX-загрузка картинки «на лету» из тулбара редактора — ещё до
    того, как само сообщение сохранено. Создаёт «осиротевшее»
    (post_id=None) is_inline-вложение; окончательно привязывается к
    сообщению при сохранении (см. _sync_inline_attachments) — если
    сообщение так и не будет сохранено, вложение почистит
    scripts/cleanup_orphan_attachments.py по cron."""
    file_storage = request.files.get("image")
    if not file_storage or not file_storage.filename:
        return jsonify(error=_("Файл не выбран.")), 400
    stored_name = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"], allowed_ext=ALLOWED_PHOTO_EXT)
    if not stored_name:
        return jsonify(error=_("Недопустимый формат файла. Разрешены: jpg, png, webp, gif.")), 400

    att = ForumAttachment(
        post_id=None,
        original_filename=file_storage.filename,
        stored_filename=stored_name,
        content_type=file_storage.content_type,
        is_inline=True,
        author_id=g.user.id,
    )
    database.db_session.add(att)
    database.db_session.commit()
    return jsonify(url=url_for("forum.attachment", attachment_id=att.id, original_filename=att.original_filename))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "GET":
        return render_template("forum/form.html")

    f = request.form
    title = f.get("title", "").strip()
    body = f.get("body", "").strip()
    if not title or not body:
        flash(_("Заполните заголовок темы и текст сообщения."), "danger")
        return redirect(url_for("forum.create"))

    topic = ForumTopic(title=title, author_id=g.user.id)
    database.db_session.add(topic)
    database.db_session.flush()

    post = ForumPost(topic_id=topic.id, body=body, author_id=g.user.id)
    database.db_session.add(post)
    database.db_session.flush()
    _sync_inline_attachments(post, body)

    audit.record(
        "forum.topic_create", entity_type="forum_topic", entity_id=topic.id,
        summary=f"Создана тема форума «{title}»",
    )
    database.db_session.commit()
    flash(_("Тема создана."), "success")
    return redirect(url_for("forum.view", topic_id=topic.id))


@bp.route("/<int:topic_id>")
@login_required
def view(topic_id):
    topic = database.db_session.get(ForumTopic, topic_id)
    if topic is None:
        abort(404)
    return render_template("forum/topic.html", topic=topic)


@bp.route("/<int:topic_id>/reply", methods=["POST"])
@login_required
def reply(topic_id):
    topic = database.db_session.get(ForumTopic, topic_id)
    if topic is None:
        abort(404)
    if topic.is_closed:
        flash(_("Тема закрыта для новых ответов."), "danger")
        return redirect(url_for("forum.view", topic_id=topic.id))

    body = request.form.get("body", "").strip()
    if not body:
        flash(_("Введите текст сообщения."), "danger")
        return redirect(url_for("forum.view", topic_id=topic.id))

    post = ForumPost(topic_id=topic.id, body=body, author_id=g.user.id)
    database.db_session.add(post)
    database.db_session.flush()
    _sync_inline_attachments(post, body)
    topic.last_activity_at = post.created_at

    audit.record(
        "forum.reply_create", entity_type="forum_post", entity_id=post.id,
        summary=f"Ответ в теме форума «{topic.title}»",
    )
    database.db_session.commit()
    return redirect(url_for("forum.view", topic_id=topic.id))


@bp.route("/posts/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def edit_post(post_id):
    post = database.db_session.get(ForumPost, post_id)
    if post is None:
        abort(404)
    if not _can_edit_post(post):
        abort(403)
    is_first = _is_first_post(post)

    if request.method == "GET":
        return render_template("forum/edit_post.html", post=post, is_first=is_first)

    f = request.form
    body = f.get("body", "").strip()
    if not body:
        flash(_("Введите текст сообщения."), "danger")
        return redirect(url_for("forum.edit_post", post_id=post.id))

    if is_first:
        title = f.get("title", "").strip()
        if not title:
            flash(_("Заполните заголовок темы."), "danger")
            return redirect(url_for("forum.edit_post", post_id=post.id))
        post.topic.title = title

    post.body = body
    _sync_inline_attachments(post, body)

    audit.record(
        "forum.post_edit", entity_type="forum_post", entity_id=post.id,
        summary=f"Изменено сообщение в теме форума «{post.topic.title}»",
    )
    database.db_session.commit()
    flash(_("Сообщение сохранено."), "success")
    return redirect(url_for("forum.view", topic_id=post.topic_id))


@bp.route("/posts/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    post = database.db_session.get(ForumPost, post_id)
    if post is None:
        abort(404)
    if not _can_manage_post(post):
        abort(403)
    if _is_first_post(post):
        flash(_("Первое сообщение темы можно удалить только вместе со всей темой."), "danger")
        return redirect(url_for("forum.view", topic_id=post.topic_id))

    topic_id = post.topic_id
    topic_title = post.topic.title
    database.db_session.delete(post)
    audit.record(
        "forum.post_delete", entity_type="forum_post", entity_id=post_id,
        summary=f"Удалено сообщение в теме форума «{topic_title}»",
    )
    database.db_session.commit()
    flash(_("Сообщение удалено."), "success")
    return redirect(url_for("forum.view", topic_id=topic_id))


@bp.route("/<int:topic_id>/delete", methods=["POST"])
@login_required
def delete_topic(topic_id):
    topic = database.db_session.get(ForumTopic, topic_id)
    if topic is None:
        abort(404)
    if not _can_manage_topic(topic):
        abort(403)

    title = topic.title
    database.db_session.delete(topic)
    audit.record(
        "forum.topic_delete", entity_type="forum_topic", entity_id=topic_id,
        summary=f"Удалена тема форума «{title}»",
    )
    database.db_session.commit()
    flash(_("Тема удалена."), "success")
    return redirect(url_for("forum.list_topics"))


@bp.route("/<int:topic_id>/close", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def close_topic(topic_id):
    topic = database.db_session.get(ForumTopic, topic_id)
    if topic is None:
        abort(404)
    topic.is_closed = True
    audit.record(
        "forum.topic_close", entity_type="forum_topic", entity_id=topic.id,
        summary=f"Закрыта тема форума «{topic.title}»",
    )
    database.db_session.commit()
    flash(_("Тема закрыта для новых ответов."), "success")
    return redirect(url_for("forum.view", topic_id=topic.id))


@bp.route("/<int:topic_id>/reopen", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def reopen_topic(topic_id):
    topic = database.db_session.get(ForumTopic, topic_id)
    if topic is None:
        abort(404)
    topic.is_closed = False
    audit.record(
        "forum.topic_reopen", entity_type="forum_topic", entity_id=topic.id,
        summary=f"Открыта тема форума «{topic.title}»",
    )
    database.db_session.commit()
    flash(_("Тема снова открыта для ответов."), "success")
    return redirect(url_for("forum.view", topic_id=topic.id))


@bp.route("/attachments/<int:attachment_id>/<path:original_filename>")
@login_required
def attachment(attachment_id, original_filename):
    """Отдаёт файл вложения. Путь включает исходное имя файла только для
    красивого URL и правильного имени при скачивании — файл на диске
    ищем по attachment_id. Весь форум только для вошедших
    (@login_required на самом роуте) — отдельной проверки видимости
    конкретного сообщения, в отличие от bulletin.py, не нужно."""
    att = database.db_session.get(ForumAttachment, attachment_id)
    if att is None:
        abort(404)
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    return send_file(
        os.path.join(upload_folder, att.stored_filename),
        as_attachment=True,
        download_name=att.original_filename,
    )
