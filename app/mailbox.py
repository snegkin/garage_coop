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
import datetime as dt
import io
import re

import bleach
from sqlalchemy.exc import IntegrityError

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, send_file, jsonify, g

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required, ROLE_LEVEL
from .models import RoleEnum, MailboxSettings, MailProtocol, MailEncryption, MailboxPop3MessageState, User
from . import mail_client
from .mail_client import MailError, DEFAULT_FOLDER, MessageDetail
from .mail_html import render_email_body
from .news_format import render_html
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


def _sort_from_request() -> tuple[str, str]:
    sort = request.args.get("sort", "date")
    sort_dir = request.args.get("dir", "desc")
    if sort not in mail_client.SORT_FIELDS:
        sort = "date"
    if sort_dir not in mail_client.SORT_DIRS:
        sort_dir = "desc"
    return sort, sort_dir


def _record_connection_error(settings: MailboxSettings, exc: MailError) -> None:
    settings.last_error = str(exc)
    database.db_session.commit()


def _sent_folder_available(settings: MailboxSettings) -> bool:
    return settings.incoming_protocol == MailProtocol.IMAP and bool(settings.sent_folder)


def _trash_folder_available(settings: MailboxSettings) -> bool:
    return settings.incoming_protocol == MailProtocol.IMAP and bool(settings.trash_folder)


def _drafts_folder_available(settings: MailboxSettings) -> bool:
    return settings.incoming_protocol == MailProtocol.IMAP and bool(settings.drafts_folder)


def _spam_folder_available(settings: MailboxSettings) -> bool:
    return settings.incoming_protocol == MailProtocol.IMAP and bool(settings.spam_folder)


# Папки, которые можно открыть через query-параметр ?folder=, кроме INBOX —
# (имя атрибута MailboxSettings, функция-проверка доступности) в порядке
# показа вкладок (см. mailbox/inbox.html). Отправленные/Черновики — с точки
# зрения _folder_from_request и вкладок ничем не отличаются от Спама/Корзины
# (просмотр); особая логика Отправленных/Корзины (APPEND после отправки,
# перемещение при удалении) живёт в других местах и на этот список не влияет.
EXTRA_FOLDERS = (
    ("sent_folder", _sent_folder_available),
    ("drafts_folder", _drafts_folder_available),
    ("spam_folder", _spam_folder_available),
    ("trash_folder", _trash_folder_available),
)


def _html_to_text(html: str) -> str:
    """Грубая конвертация HTML в текст для цитирования оригинала письма,
    у которого нет text/plain-альтернативы (см. compose(): reply_to/forward).
    Не претендует на точность — только чтобы в цитате не остались теги,
    переносы строк расставлены по <br>/</p>, а не всё одним куском."""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = bleach.clean(text, tags=[], strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _plain_text_of(detail: MessageDetail) -> str:
    if detail.body_text:
        return detail.body_text
    if detail.body_html:
        return _html_to_text(detail.body_html)
    return ""


def _reply_prefill(detail: MessageDetail) -> tuple[str, str, str]:
    """(to, subject, body) для «Ответить» — получатель - отправитель
    оригинала, тема с «Re:» (без дублирования, если оно уже есть), тело -
    пустая первая строка для ответа + цитата оригинала с «> »."""
    to = detail.from_addr or ""  # реальный адрес получателя ответа — НЕ декодировать, это значение поля формы, а не подпись для человека
    subject = detail.subject if detail.subject.lower().startswith("re:") else _("Re: {subject}", subject=detail.subject)
    when = detail.date.strftime("%d.%m.%Y %H:%M") if detail.date else ""
    who = detail.from_name or mail_client.decode_idn_address(detail.from_addr)
    header = _("{when} {who} писал(а):", when=when, who=who) if (when or who) else _("Исходное письмо:")
    quoted = "\n".join("> " + line for line in _plain_text_of(detail).splitlines())
    body = "\n\n" + header + "\n" + quoted + "\n"
    return to, subject, body


def _forward_prefill(detail: MessageDetail) -> tuple[str, str, str]:
    """(to, subject, body) для «Переслать» — получателя правление выбирает
    само (пусто), тема с «Fwd:», тело — служебный заголовок + текст
    оригинала БЕЗ цитирования (">"), вложения оригинала не переносятся
    автоматически (это отдельная непростая механика — переучёт cid/повторная
    загрузка через IMAP), только упоминаются в тексте, чтобы автор письма не
    удивился их отсутствию и при необходимости прикрепил вручную."""
    subject = detail.subject if detail.subject.lower().startswith("fwd:") else _("Fwd: {subject}", subject=detail.subject)
    when = detail.date.strftime("%d.%m.%Y %H:%M") if detail.date else "—"
    who = detail.from_name or ""
    from_addr_display = mail_client.decode_idn_address(detail.from_addr)
    from_line = f"{who} <{from_addr_display}>" if who and from_addr_display else (from_addr_display or who or "—")
    to_display = ", ".join(mail_client.decode_idn_address(a) for a in detail.to_addrs) or "—"
    body = (
        "\n\n---------- " + _("Пересланное сообщение") + " ----------\n"
        + _("От") + f": {from_line}\n"
        + _("Дата") + f": {when}\n"
        + _("Тема") + f": {detail.subject}\n"
        + _("Кому") + f": {to_display}\n\n"
        + _plain_text_of(detail)
    )
    if detail.attachments:
        names = ", ".join(a.filename for a in detail.attachments)
        body += "\n\n" + _("[Вложения оригинала не пересылаются автоматически: {names} — прикрепите вручную при необходимости.]", names=names)
    return "", subject, body


def _folder_from_request(settings: MailboxSettings) -> str:
    """Папка — только «Входящие» или (для IMAP, если настроены) «Отправленные»/
    «Корзина», никогда произвольная строка из query — так UI не даёт зайти в
    папку, которую сам же не показывает и для которой не строит ссылки (в
    частности для POP3, где папок нет вовсе)."""
    requested = request.values.get("folder", DEFAULT_FOLDER)
    for field_name, available in EXTRA_FOLDERS:
        if requested == getattr(settings, field_name) and available(settings):
            return requested
    return DEFAULT_FOLDER


def _folder_availability(settings: MailboxSettings) -> dict:
    return {f"{field_name}_available": available(settings) for field_name, available in EXTRA_FOLDERS}


# ---------------------------------------------------------------------------
# Эмуляция «прочитано»/«важное» для POP3 — сам протокол не хранит эти флаги
# на сервере (в отличие от IMAP), поэтому статус персональный для каждого
# члена правления и живёт в своей таблице MailboxPop3MessageState (см.
# app/models.py), ключ — POP3 UIDL (mail_client.Pop3MailClient.get_uidl_map),
# не MessageSummary.uid (тот — номер письма в текущей сессии, нестабилен).
# ---------------------------------------------------------------------------

def _overlay_pop3_states(user_id: int, messages: list) -> None:
    """Мутирует seen/flagged каждого MessageSummary по персональным
    пометкам ТЕКУЩЕГО пользователя — без строки в БД считается непрочитано,
    неважное (см. докстринг MailboxPop3MessageState). Письма без uidl
    (сервер не поддерживает UIDL) не трогаются — supports_message_state в
    этом случае и так False, дальше по коду точка для них не рисуется."""
    uidls = [m.uidl for m in messages if m.uidl]
    if not uidls:
        return
    rows = database.db_session.query(MailboxPop3MessageState).filter(
        MailboxPop3MessageState.user_id == user_id,
        MailboxPop3MessageState.message_uidl.in_(uidls),
    ).all()
    states_by_uidl = {row.message_uidl: row for row in rows}
    for m in messages:
        if not m.uidl:
            continue
        state = states_by_uidl.get(m.uidl)
        m.seen = bool(state.seen) if state else False
        m.flagged = bool(state.flagged) if state else False


def _pop3_state_row(user_id: int, uidl: str) -> MailboxPop3MessageState:
    row = database.db_session.query(MailboxPop3MessageState).filter_by(user_id=user_id, message_uidl=uidl).first()
    if row is None:
        row = MailboxPop3MessageState(user_id=user_id, message_uidl=uidl)
        database.db_session.add(row)
    return row


def _set_pop3_message_state(user_id: int, uidl: str, state: str) -> None:
    """Целевое состояние — та же семантика трёх состояний, что и у
    ImapMailClient.set_state (см. mail_client.MESSAGE_STATES), только
    флаги пишутся не в IMAP-команду, а строкой в свою таблицу."""
    row = _pop3_state_row(user_id, uidl)
    if state == mail_client.STATE_UNREAD:
        row.seen, row.flagged = False, False
    elif state == mail_client.STATE_READ:
        row.seen, row.flagged = True, False
    else:  # STATE_IMPORTANT — seen не трогаем, как и у IMAP-варианта
        row.flagged = True


def _mark_pop3_message_seen(user_id: int, uidl: str) -> None:
    """При открытии письма (view_message) — аналог неявной простановки
    \\Seen у IMAP при обычном (не PEEK) FETCH: только seen, flagged не
    трогаем (важное письмо не перестаёт быть важным от того, что его
    прочитали)."""
    row = _pop3_state_row(user_id, uidl)
    row.seen = True


# ---------------------------------------------------------------------------
# Бейдж «непрочитано» в шапке сайта (app/__init__.py: _inject_user) — сама
# логика подсчёта здесь, а не в scripts/poll_mailbox.py (та же схема, что у
# app/notifications.py:run_board_chat_digest / scripts/board_chat_digest.py):
# скрипт — тонкая cron-обёртка, вызывающая эту функцию раз в 5 минут; те же
# роуты выше вызывают её опортунистически при заходе в почту.
# ---------------------------------------------------------------------------

def _board_plus_users() -> list[User]:
    """Тот же минимальный уровень доступа, что и у /mailbox/ (см.
    auth.roles_required(RoleEnum.BOARD)) — не только точное совпадение роли."""
    qualifying_roles = [role for role, level in ROLE_LEVEL.items() if level >= ROLE_LEVEL[RoleEnum.BOARD]]
    return database.db_session.query(User).filter(User.role.in_(qualifying_roles)).all()


def _refresh_imap_unread_count(settings: MailboxSettings) -> str:
    try:
        with mail_client.get_incoming_client(settings) as client:
            unread = client.count_unread(DEFAULT_FOLDER)
    except MailError as exc:
        _record_connection_error(settings, exc)
        settings.last_checked_at = dt.datetime.utcnow()
        database.db_session.commit()
        return f"Ошибка подключения к почте: {exc}"

    settings.unread_count = unread
    settings.last_error = None
    settings.last_checked_at = dt.datetime.utcnow()
    database.db_session.commit()
    return f"Непрочитанных писем: {unread}."


def _refresh_pop3_unread_counts(settings: MailboxSettings) -> str:
    try:
        with mail_client.get_incoming_client(settings) as client:
            current_uidls = client.list_current_uidls(DEFAULT_FOLDER)
    except MailError as exc:
        _record_connection_error(settings, exc)
        settings.last_checked_at = dt.datetime.utcnow()
        database.db_session.commit()
        return f"Ошибка подключения к почте: {exc}"

    users = _board_plus_users()
    if current_uidls is None:
        for user in users:
            user.pop3_mailbox_unread_count = 0
        settings.last_error = None
        settings.last_checked_at = dt.datetime.utcnow()
        database.db_session.commit()
        return "Сервер не поддерживает UIDL — эмуляция статуса писем недоступна."

    for user in users:
        has_any_state = database.db_session.query(MailboxPop3MessageState.id).filter_by(user_id=user.id).first() is not None
        if not has_any_state:
            # Бутстрап — первый прогон для этого пользователя: вся история
            # на этот момент считается уже прочитанной, иначе при
            # подключении POP3-почты со старой историей все увидели бы
            # внезапный шквал "непрочитанных" за годы, которых никто не
            # пропускал. Непрочитанными для бейджа считаются только письма,
            # появившиеся ПОСЛЕ этого момента.
            #
            # Коммит сразу и per-пользователь (не одним общим коммитом в
            # конце) — если два прогона (параллельный cron + опортунистическое
            # обновление) одновременно бутстрапят ОДНОГО И ТОГО ЖЕ пользователя,
            # второй наткнётся на UniqueConstraint(user_id, message_uidl) уже
            # здесь, а не свалит коммит остальных пользователей в этом же
            # прогоне — откатываем только эту вставку и считаем по тому,
            # что реально есть в БД (см. ниже).
            try:
                database.db_session.bulk_save_objects([
                    MailboxPop3MessageState(user_id=user.id, message_uidl=uidl, seen=True) for uidl in current_uidls
                ])
                database.db_session.commit()
            except IntegrityError:
                database.db_session.rollback()

        seen_uidls = {
            row.message_uidl for row in database.db_session.query(MailboxPop3MessageState.message_uidl)
            .filter(MailboxPop3MessageState.user_id == user.id, MailboxPop3MessageState.seen.is_(True))
            .all()
        }
        user.pop3_mailbox_unread_count = len(current_uidls - seen_uidls)

    settings.last_error = None
    settings.last_checked_at = dt.datetime.utcnow()
    database.db_session.commit()
    return f"Писем в ящике: {len(current_uidls)}, пользователей обновлено: {len(users)}."


def refresh_unread_counts() -> str:
    """Пересчитывает бейдж «непрочитано» — см. scripts/poll_mailbox.py
    (cron, раз в 5 минут) и mailbox.inbox()/view_message()/set_message_state()
    (опортунистически). IMAP — общий MailboxSettings.unread_count; POP3 —
    персональный User.pop3_mailbox_unread_count (сам протокол флагов не
    хранит, см. MailboxPop3MessageState). Возвращает строку для лога
    вызывающего кода — не поднимает исключений (ошибка подключения тоже
    успешно обработанный, просто неудачный, результат)."""
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        return "Почта правления ещё не настроена — опрос пропущен."
    if settings.incoming_protocol == MailProtocol.IMAP:
        return _refresh_imap_unread_count(settings)
    return _refresh_pop3_unread_counts(settings)


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def inbox():
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        return render_template(
            "mailbox/inbox.html", settings=settings, is_configured=False, page=None,
            folder=DEFAULT_FOLDER, **{f"{field_name}_available": False for field_name, _avail in EXTRA_FOLDERS},
        )

    folder = _folder_from_request(settings)
    page_num = request.args.get("page", 1, type=int)
    page_size = _page_size_from_request()
    search = request.args.get("q", "").strip()
    sort, sort_dir = _sort_from_request()

    try:
        with mail_client.get_incoming_client(settings) as client:
            page = client.list_messages(
                page=page_num, page_size=page_size, folder=folder,
                search=search or None, sort=sort, sort_dir=sort_dir,
            )
            supports_flags = client.supports_flags
            # POP3-эмуляция статуса (см. _overlay_pop3_states выше) — только
            # если сервер реально поддерживает UIDL (client.supports_message_state,
            # выставляется list_messages() по факту), иначе для POP3 точка в
            # списке не рисуется вовсе (supports_flags остаётся False).
            pop3_tracked = settings.incoming_protocol == MailProtocol.POP3 and client.supports_message_state
            if pop3_tracked:
                _overlay_pop3_states(g.user.id, page.messages)

            # Бейдж "непрочитано" в шапке сайта (см. app/__init__.py:
            # _inject_user) в норме обновляется cron-скриптом
            # scripts/poll_mailbox.py (mailbox.refresh_unread_counts) — здесь
            # просто освежаем его же кэш заодно, раз соединение с INBOX и
            # так уже открыто; НЕ вызываем refresh_unread_counts() напрямую
            # — та открывает СВОЁ соединение, что здесь удвоило бы поход на
            # сервер вместо переиспользования уже открытого. Ошибка — не
            # повод ломать обычный просмотр списка, бейдж просто останется
            # чуть устаревшим до следующего прогона cron.
            if folder == DEFAULT_FOLDER and supports_flags:
                try:
                    settings.unread_count = client.count_unread(folder)
                    settings.last_checked_at = dt.datetime.utcnow()
                    database.db_session.commit()
                except MailError:
                    database.db_session.rollback()
            elif folder == DEFAULT_FOLDER and pop3_tracked:
                try:
                    current_uidls = client.list_current_uidls(folder)
                    seen_uidls = {
                        row.message_uidl for row in database.db_session.query(MailboxPop3MessageState.message_uidl)
                        .filter(MailboxPop3MessageState.user_id == g.user.id, MailboxPop3MessageState.seen.is_(True))
                        .all()
                    }
                    g.user.pop3_mailbox_unread_count = len(current_uidls - seen_uidls) if current_uidls is not None else 0
                    database.db_session.commit()
                except MailError:
                    database.db_session.rollback()
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось подключиться к почте: {error}", error=str(exc)), "danger")
        return render_template(
            "mailbox/inbox.html", settings=settings, is_configured=True, page=None, folder=folder,
            search=search, sort=sort, sort_dir=sort_dir, page_size=page_size,
            **_folder_availability(settings),
        )

    return render_template(
        "mailbox/inbox.html", settings=settings, is_configured=True, page=page, folder=folder,
        page_size=page_size, page_size_choices=PAGE_SIZE_CHOICES, supports_flags=(supports_flags or pop3_tracked),
        search=search, sort=sort, sort_dir=sort_dir,
        **_folder_availability(settings),
    )


@bp.route("/messages/<uid>")
@roles_required(RoleEnum.BOARD)
def view_message(uid):
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        flash(_("Почта ещё не настроена."), "warning")
        return redirect(url_for("mailbox.inbox"))

    folder = _folder_from_request(settings)
    # Внешние картинки показываются по умолчанию — ГСК не нужна защита от
    # трекинг-пикселей (не публичная организация, нет причин ожидать
    # целенаправленной слежки за прочтением писем правлением). ?allow_images=0
    # оставлен как обратный переключатель — сама защита в mail_html.py не
    # убрана, просто дефолт другой.
    allow_images = request.args.get("allow_images", "1") == "1"
    try:
        with mail_client.get_incoming_client(settings) as client:
            detail = client.get_message(uid, folder=folder)
            # Аналог неявной простановки \Seen у IMAP при обычном (не PEEK)
            # FETCH — см. _mark_pop3_message_seen. Персонально для текущего
            # пользователя, только если сервер поддерживает UIDL.
            if settings.incoming_protocol == MailProtocol.POP3:
                uidl_map = client.get_uidl_map()
                message_uidl = uidl_map.get(int(uid)) if uidl_map else None
                if message_uidl:
                    _mark_pop3_message_seen(g.user.id, message_uidl)
                    database.db_session.commit()
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось открыть письмо: {error}", error=str(exc)), "danger")
        return redirect(url_for("mailbox.inbox", folder=folder))

    body_srcdoc, had_blocked_images = render_email_body(detail, allow_remote_images=allow_images)
    is_trash = folder == settings.trash_folder and _trash_folder_available(settings)
    return render_template(
        "mailbox/message.html", detail=detail, body_srcdoc=body_srcdoc, folder=folder,
        had_blocked_images=had_blocked_images, allow_images=allow_images,
        will_permanently_delete=is_trash or not _trash_folder_available(settings),
    )


@bp.route("/messages/<uid>/attachments/<int:index>")
@roles_required(RoleEnum.BOARD)
def download_attachment(uid, index):
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        abort(404)

    folder = _folder_from_request(settings)
    try:
        with mail_client.get_incoming_client(settings) as client:
            part, data = client.get_attachment(uid, index, folder=folder)
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось скачать вложение: {error}", error=str(exc)), "danger")
        return redirect(url_for("mailbox.view_message", uid=uid, folder=folder))

    return send_file(
        io.BytesIO(data), mimetype=part.content_type or "application/octet-stream",
        as_attachment=True, download_name=part.filename,
    )


@bp.route("/messages/<uid>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_message(uid):
    """Если настроена «Корзина» и письмо не в ней самой — перемещаем туда
    (можно достать обратно вручную через почтовый клиент/веб-интерфейс
    провайдера), иначе (нет «Корзины», POP3, или письмо уже в «Корзине») —
    как раньше, безвозвратное удаление."""
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        flash(_("Почта ещё не настроена."), "warning")
        return redirect(url_for("mailbox.inbox"))

    folder = _folder_from_request(settings)
    trash_available = _trash_folder_available(settings)
    move_to_trash = trash_available and folder != settings.trash_folder

    try:
        with mail_client.get_incoming_client(settings) as client:
            if move_to_trash:
                client.move_message(uid, folder, settings.trash_folder)
            else:
                client.delete_message(uid, folder=folder)
    except MailError as exc:
        _record_connection_error(settings, exc)
        flash(_("Не удалось удалить письмо: {error}", error=str(exc)), "danger")
        return redirect(url_for("mailbox.view_message", uid=uid, folder=folder))

    if move_to_trash:
        audit.record("mailbox.message_trash", f"Письмо из папки «{folder}» перемещено в «{settings.trash_folder}»")
        flash(_("Письмо перемещено в корзину."), "success")
    else:
        audit.record("mailbox.message_delete", f"Удалено письмо из папки «{folder}»")
        flash(_("Письмо удалено."), "success")
    database.db_session.commit()
    return redirect(url_for("mailbox.inbox", folder=folder))


@bp.route("/messages/<uid>/state", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def set_message_state(uid):
    """Точка-индикатор в списке писем (см. mailbox/inbox.html) — цикл
    непрочитано -> прочитано -> важное -> непрочитано, см.
    mail_client.MESSAGE_STATES. IMAP — настоящие серверные флаги; POP3 —
    эмуляция в своей таблице по UIDL (см. _set_pop3_message_state), только
    если сервер поддерживает это расширение."""
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        return jsonify(ok=False, error=_("Почта ещё не настроена.")), 400

    state = request.form.get("state", "")
    if state not in mail_client.MESSAGE_STATES:
        return jsonify(ok=False, error=_("Неизвестное состояние письма.")), 400

    folder = _folder_from_request(settings)
    try:
        with mail_client.get_incoming_client(settings) as client:
            if client.supports_flags:
                client.set_state(uid, state, folder=folder)
            elif settings.incoming_protocol == MailProtocol.POP3:
                uidl_map = client.get_uidl_map()
                message_uidl = uidl_map.get(int(uid)) if uidl_map else None
                if message_uidl is None:
                    return jsonify(ok=False, error=_("Эта почта не поддерживает статусы писем.")), 400
                _set_pop3_message_state(g.user.id, message_uidl, state)
                database.db_session.commit()
            else:
                return jsonify(ok=False, error=_("Этот протокол не поддерживает статусы писем.")), 400
    except MailError as exc:
        _record_connection_error(settings, exc)
        return jsonify(ok=False, error=str(exc)), 400

    return jsonify(ok=True, state=state)


@bp.route("/compose", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def compose():
    settings = _get_or_create_settings()
    if not _is_configured(settings):
        flash(_("Почта ещё не настроена."), "warning")
        return redirect(url_for("mailbox.inbox"))

    if request.method == "POST":
        # Отправляется через fetch (см. mailbox/compose.html) — при ошибке
        # (например опечатка в адресе) страница НЕ перезагружается, поэтому
        # выбранные файлы в <input type="file"> не сбрасываются (браузер
        # безусловно очищает их при любой навигации/повторном рендере
        # формы — восстановить значение поля файла программно нельзя даже
        # через JS, только сам пользователь может выбрать файл заново).
        f = request.form
        to_raw = f.get("to", "").strip()
        subject = f.get("subject", "").strip()
        body = f.get("body", "")
        to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]

        if not to_addrs or not subject:
            return jsonify(ok=False, error=_("Укажите получателя и тему письма.")), 400

        attachments = []
        for file_storage in request.files.getlist("attachments"):
            if not file_storage or not file_storage.filename:
                continue
            attachments.append((file_storage.filename, file_storage.content_type or "application/octet-stream", file_storage.read()))

        # Тело письма пишется в той же упрощённой markdown-разметке, что и
        # новости/вики (тулбар в compose.html) — на выходе реальный HTML
        # (жирный/курсив/списки и т.п. в почтовом клиенте получателя), текст
        # без разметки остаётся как plain-fallback (см. mail_client.send_message).
        body_html = str(render_html(body)) if body.strip() else None

        try:
            mail_client.send_message(
                settings, to_addrs=to_addrs, subject=subject, body_text=body, body_html=body_html,
                attachments=attachments,
            )
        except MailError as exc:
            _record_connection_error(settings, exc)
            return jsonify(ok=False, error=_("Не удалось отправить письмо: {error}", error=str(exc))), 400

        audit.record("mailbox.message_sent", f"Отправлено письмо на {', '.join(to_addrs)}: «{subject}»")
        database.db_session.commit()
        flash(_("Письмо отправлено."), "success")  # подхватится при переходе на inbox из JS
        return jsonify(ok=True, redirect=url_for("mailbox.inbox"))

    to, subject, body = "", "", ""
    reply_uid = request.args.get("reply_to")
    forward_uid = request.args.get("forward")
    if reply_uid or forward_uid:
        folder = _folder_from_request(settings)
        try:
            with mail_client.get_incoming_client(settings) as client:
                detail = client.get_message(reply_uid or forward_uid, folder=folder)
        except MailError as exc:
            _record_connection_error(settings, exc)
            flash(_("Не удалось открыть письмо: {error}", error=str(exc)), "danger")
            return redirect(url_for("mailbox.inbox", folder=folder))
        to, subject, body = _reply_prefill(detail) if reply_uid else _forward_prefill(detail)

    return render_template("mailbox/compose.html", to=to, subject=subject, body=body)


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
    settings.sent_folder = f.get("sent_folder", "").strip() or None
    settings.trash_folder = f.get("trash_folder", "").strip() or None
    settings.drafts_folder = f.get("drafts_folder", "").strip() or None
    settings.spam_folder = f.get("spam_folder", "").strip() or None

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
