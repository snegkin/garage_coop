"""
Почта правления (`/mailbox/`) — общий почтовый ящик (напр. pravlenie@...),
не личная почта отдельных членов. Правление (BOARD) читает и пишет письма,
настройки подключения (IMAP/POP3 + SMTP) меняет только председатель
(CHAIRMAN) — тот же принцип разделения прав, что у app/bank_sync.py и
app/electricity_monitor.py.

Письма нигде не кэшируются в БД — каждый просмотр/отправка идёт живым
подключением к серверу (см. app/mail_client.py). Настройки — синглтон
MailboxSettings (app/models.py), пароль общий для входящих и SMTP,
шифруется через app/bank_api/crypto.py (общего назначения, несмотря на путь).

HTML-тело письма рендерится в песочнице (iframe sandbox) после
санитайзинга — см. app/mail_html.py, там же подробно про блокировку
внешних картинок (защита от трекинг-пикселей).
"""
import io

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, send_file

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, MailboxSettings, MailProtocol, MailEncryption
from . import mail_client
from .mail_client import MailError
from .mail_html import render_email_body
from .bank_api import crypto

bp = Blueprint("mailbox", __name__, url_prefix="/mailbox")

PAGE_SIZE_CHOICES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 25


def _get_or_create_settings() -> MailboxSettings:
    settings = database.db_session.query(MailboxSettings).first()
    if settings is None:
        settings = MailboxSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


def _is_configured(settings: MailboxSettings) -> bool:
    return bool(settings.incoming_host and settings.username and settings.password_encrypted)


def _page_size_from_request() -> int:
    raw = request.args.get("page_size", DEFAULT_PAGE_SIZE, type=int)
    return raw if raw in PAGE_SIZE_CHOICES else DEFAULT_PAGE_SIZE


def _record_connection_error(settings: MailboxSettings, exc: MailError) -> None:
    settings.last_error = str(exc)
    database.db_session.commit()


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def inbox():
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        return render_template("mailbox/inbox.html", settings=settings, is_configured=False, page=None)

    page_num = request.args.get("page", 1, type=int)
    page_size = _page_size_from_request()

    try:
        with mail_client.get_incoming_client(settings) as client:
            page = client.list_messages(page=page_num, page_size=page_size)
            supports_flags = client.supports_flags
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось подключиться к почте: {error}", error=str(exc)), "danger")
        return render_template("mailbox/inbox.html", settings=settings, is_configured=True, page=None)

    return render_template(
        "mailbox/inbox.html", settings=settings, is_configured=True, page=page,
        page_size=page_size, page_size_choices=PAGE_SIZE_CHOICES, supports_flags=supports_flags,
    )


@bp.route("/messages/<uid>")
@roles_required(RoleEnum.BOARD)
def view_message(uid):
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        flash(_("Почта ещё не настроена."), "warning")
        return redirect(url_for("mailbox.inbox"))

    allow_images = request.args.get("allow_images") == "1"
    try:
        with mail_client.get_incoming_client(settings) as client:
            detail = client.get_message(uid)
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось открыть письмо: {error}", error=str(exc)), "danger")
        return redirect(url_for("mailbox.inbox"))

    body_srcdoc, had_blocked_images = render_email_body(detail, allow_remote_images=allow_images)
    return render_template(
        "mailbox/message.html", detail=detail, body_srcdoc=body_srcdoc,
        had_blocked_images=had_blocked_images, allow_images=allow_images,
    )


@bp.route("/messages/<uid>/attachments/<int:index>")
@roles_required(RoleEnum.BOARD)
def download_attachment(uid, index):
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        abort(404)

    try:
        with mail_client.get_incoming_client(settings) as client:
            part, data = client.get_attachment(uid, index)
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось скачать вложение: {error}", error=str(exc)), "danger")
        return redirect(url_for("mailbox.view_message", uid=uid))

    return send_file(
        io.BytesIO(data), mimetype=part.content_type or "application/octet-stream",
        as_attachment=True, download_name=part.filename,
    )


@bp.route("/compose", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def compose():
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        flash(_("Почта ещё не настроена."), "warning")
        return redirect(url_for("mailbox.inbox"))

    if request.method == "POST":
        f = request.form
        to_raw = f.get("to", "").strip()
        subject = f.get("subject", "").strip()
        body = f.get("body", "")
        to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]

        if not to_addrs or not subject:
            flash(_("Укажите получателя и тему письма."), "danger")
            return render_template("mailbox/compose.html", to=to_raw, subject=subject, body=body)

        attachments = []
        for file_storage in request.files.getlist("attachments"):
            if not file_storage or not file_storage.filename:
                continue
            attachments.append((file_storage.filename, file_storage.content_type or "application/octet-stream", file_storage.read()))

        try:
            mail_client.send_message(settings, to_addrs=to_addrs, subject=subject, body_text=body, attachments=attachments)
        except MailError as exc:
            _record_connection_error(settings, exc)
            flash(_("Не удалось отправить письмо: {error}", error=str(exc)), "danger")
            return render_template("mailbox/compose.html", to=to_raw, subject=subject, body=body)

        audit.record("mailbox.message_sent", f"Отправлено письмо на {', '.join(to_addrs)}: «{subject}»")
        database.db_session.commit()
        flash(_("Письмо отправлено."), "success")
        return redirect(url_for("mailbox.inbox"))

    return render_template("mailbox/compose.html", to="", subject="", body="")


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """Пустое поле пароля в форме = не менять (как в bank_sync.save_api_settings/
    electricity_monitor.save_settings) — иначе председателю пришлось бы
    вводить пароль заново при каждой правке любого другого поля."""
    settings = _get_or_create_settings()
    f = request.form

    try:
        settings.incoming_protocol = MailProtocol(f.get("incoming_protocol"))
    except ValueError:
        settings.incoming_protocol = MailProtocol.IMAP
    settings.incoming_host = f.get("incoming_host", "").strip() or None
    settings.incoming_port = int(f["incoming_port"]) if f.get("incoming_port") else (993 if settings.incoming_protocol == MailProtocol.IMAP else 995)
    try:
        settings.incoming_encryption = MailEncryption(f.get("incoming_encryption"))
    except ValueError:
        settings.incoming_encryption = MailEncryption.SSL

    settings.smtp_host = f.get("smtp_host", "").strip() or None
    settings.smtp_port = int(f["smtp_port"]) if f.get("smtp_port") else 587
    try:
        settings.smtp_encryption = MailEncryption(f.get("smtp_encryption"))
    except ValueError:
        settings.smtp_encryption = MailEncryption.STARTTLS

    settings.username = f.get("username", "").strip() or None
    settings.from_name = f.get("from_name", "").strip() or None

    password = f.get("password", "")
    if password:
        settings.password_encrypted = crypto.encrypt(password)

    settings.last_error = None

    audit.record(
        "mailbox.settings_save",
        f"Настройки почты правления обновлены (входящая: {settings.incoming_protocol.value} "
        f"{settings.incoming_host}:{settings.incoming_port}, SMTP: {settings.smtp_host}:{settings.smtp_port})",
    )
    database.db_session.commit()
    flash(_("Настройки почты сохранены."), "success")
    return redirect(url_for("mailbox.inbox"))


@bp.route("/settings/test-connection", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def test_connection():
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        flash(_("Сначала заполните и сохраните настройки подключения."), "warning")
        return redirect(url_for("mailbox.inbox"))

    errors = []
    try:
        mail_client.test_incoming_connection(settings)
    except MailError as exc:
        errors.append(str(exc))

    try:
        mail_client.test_smtp_connection(settings)
    except MailError as exc:
        errors.append(str(exc))

    if errors:
        settings.last_error = "; ".join(errors)
        database.db_session.commit()
        flash(_("Ошибка подключения: {error}", error="; ".join(errors)), "danger")
    else:
        settings.last_error = None
        database.db_session.commit()
        flash(_("Подключение к почте работает (входящие и исходящие)."), "success")

    return redirect(url_for("mailbox.inbox"))
