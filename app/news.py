import datetime as dt
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    g, current_app, send_file, jsonify,
)

from . import audit
from . import database
from . import notifications
from . import max_bot
from . import telegram_bot
from . import messenger_outbox
from .i18n import translate as _
from .auth import roles_required
from .models import News, NewsAttachment, RoleEnum, MessengerOutbox, MessengerOutboxStatus
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
    """Последние новости для отображения на главной (см. app/main.py:
    index()) — сама лента и есть управление новостями: отдельной страницы
    со списком нет, добавление/правка/удаление — кнопками прямо в ленте
    (см. home.html), видны только правлению (is_board())."""
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


@bp.route("/preview", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def preview():
    """AJAX-предпросмотр markdown из тулбара формы новости (см.
    news/form.html) — рендерит текущий текст textarea ровно тем же
    render_html(), что и итоговая опубликованная статья (не отдельный
    JS-рендерер markdown — рискует разойтись с настоящим выводом), без
    сохранения черновика."""
    return jsonify(html=render_html(request.form.get("body", "")))


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
        database.db_session.flush()
        audit.record("news.create", f"Опубликована новость «{item.title}»", entity_type="news", entity_id=item.id)
        database.db_session.commit()
        notifications.notify_subscribers(
            "news", "Новая новость", item.title, exclude_user_id=g.user.id, board_group="documents",
        )
        flash(_("Новость добавлена."), "success")
        for channel in news_channels():
            if f.get(f"publish_{channel['id']}"):
                _publish(item, channel)
        return redirect(url_for("main.index"))

    return render_template("news/form.html", item=None, news_channels=news_channels())


# Куда можно дублировать новости: id совпадает с MessengerOutbox.channel и
# префиксом полей формы (telegram_silent, max_send_at_utc…).
_CHANNELS = [
    {"id": messenger_outbox.CHANNEL_TELEGRAM, "label": "Telegram", "icon": "bi-telegram", "bot": telegram_bot},
    {"id": messenger_outbox.CHANNEL_MAX, "label": "MAX", "icon": "bi-send", "bot": max_bot},
]


def news_channels() -> list[dict]:
    """Мессенджеры, в которые настроено дублирование новостей (бот и группа
    заданы) — для формы новости и меню «Отправить в …» в ленте."""
    return [c for c in _CHANNELS if c["bot"].news_group_enabled(c["bot"].get_settings())]


def _channel(channel_id: str) -> dict | None:
    return next((c for c in news_channels() if c["id"] == channel_id), None)


def _parse_utc(value: str | None) -> dt.datetime | None:
    """Время отложенной отправки из формы: браузер переводит выбранное
    местное время в UTC ISO-строку (toISOString, см. news/form.html) —
    так часовой пояс сервера ни при чём. Битое значение — None (сразу)."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _publish(item: News, channel: dict) -> None:
    """Ставит новость в очередь отправки в мессенджер channel
    (app/messenger_outbox.py) — уйдёт с ближайшим запуском cron
    scripts/poll_telegram.py, с повторами, если сервис недоступен. Варианты —
    из полей формы с префиксом channel["id"] (news/_send_options.html)."""
    prefix = channel["id"]
    send_at = _parse_utc(request.form.get(f"{prefix}_send_at_utc"))
    out = messenger_outbox.enqueue_news(
        item, url_for("news.view", news_id=item.id, _external=True),
        album_first=bool(request.form.get("telegram_album_first")) and prefix == messenger_outbox.CHANNEL_TELEGRAM,
        silent=bool(request.form.get(f"{prefix}_silent")),
        send_at=send_at,
        channel=prefix,
    )
    database.db_session.commit()
    if out.scheduled_at:
        flash(_("Пост в {channel} запланирован — его можно отменить в очереди на странице настроек {channel}.",
                channel=channel["label"]), "info")
    split = (telegram_bot._split_attachments if prefix == messenger_outbox.CHANNEL_TELEGRAM
             else max_bot.split_attachments)
    _photos, _documents, skipped = split(item, current_app.config["UPLOAD_FOLDER"])
    if skipped:
        flash(_("В {channel} не отправлены файлы, которые не принимает мессенджер (слишком большие): {names}",
                channel=channel["label"], names=", ".join(skipped)), "warning")


@bp.route("/<int:news_id>/<any(telegram, max):channel_id>", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def send_to_channel(news_id, channel_id):
    """«Отправить в Telegram/MAX» из меню новости в ленте — для уже
    опубликованных (в том числе до появления бота или если при создании
    галочку сняли). Те же варианты, что в форме новой новости; прошлые
    отправки этой новости туда же показываются, чтобы не задвоить пост."""
    item = database.db_session.get(News, news_id)
    if item is None:
        abort(404)
    channel = _channel(channel_id)
    if channel is None:
        flash(_("Группа для новостей в этом мессенджере не настроена."), "warning")
        return redirect(url_for("main.index"))

    if request.method == "POST":
        _publish(item, channel)
        if not request.form.get(f"{channel_id}_send_at_utc"):
            flash(_("Новость поставлена в очередь — в группе появится в течение минуты."), "success")
        audit.record(f"news.{channel_id}", f"Новость «{item.title}» отправлена в {channel['label']}",
                     entity_type="news", entity_id=item.id)
        database.db_session.commit()
        return redirect(url_for("main.index"))

    history = (
        database.db_session.query(MessengerOutbox)
        .filter(MessengerOutbox.news_id == item.id, MessengerOutbox.channel == channel_id,
                MessengerOutbox.status != MessengerOutboxStatus.CANCELLED)
        .order_by(MessengerOutbox.id.desc())
        .all()
    )
    order_visible = False
    if channel_id == messenger_outbox.CHANNEL_TELEGRAM:
        photos, _documents, _skipped = telegram_bot._split_attachments(item, current_app.config["UPLOAD_FOLDER"])
        order_visible = bool(photos) and not telegram_bot.fits_caption(item.title, item.body)
    # Окно на главной подгружает только тело (X-Requested-With), по прямой
    # ссылке — целая страница.
    template = ("news/_send_body.html"
                if request.headers.get("X-Requested-With") == "XMLHttpRequest" else "news/send.html")
    return render_template(
        template, item=item, history=history, order_visible=order_visible,
        channel=channel_id, channel_label=channel["label"], channel_icon=channel["icon"],
    )


@bp.route("/telegram-layout", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def telegram_layout():
    """Форма новости спрашивает при наборе: влезет ли текст подписью к
    альбому (один пост) — если нет, показывает выбор порядка двух сообщений.
    Считается тем же кодом, что потом собирает пост (telegram_bot.fits_caption)."""
    f = request.form
    return jsonify(fits_caption=telegram_bot.fits_caption(f.get("title", ""), f.get("body", "")))


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
        audit.record("news.edit", f"Изменена новость «{item.title}»", entity_type="news", entity_id=item.id)
        database.db_session.commit()
        flash(_("Новость обновлена."), "success")
        return redirect(url_for("main.index"))

    return render_template("news/form.html", item=item)


@bp.route("/<int:news_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(news_id):
    item = database.db_session.get(News, news_id)
    if item is None:
        abort(404)
    audit.record("news.delete", f"Удалена новость «{item.title}»")
    database.db_session.delete(item)
    database.db_session.commit()
    flash(_("Новость удалена."), "success")
    return redirect(url_for("main.index"))


@bp.route("/<int:news_id>")
def view(news_id):
    """Полная новость отдельной страницей — публичная (доступна и
    анонимным посетителям), но никуда не заведена ссылкой (см. главную,
    app/main.py: index() — там теперь всегда полный текст статьи прямо в
    ленте, разворачивается на месте); прямой URL по-прежнему работает."""
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
