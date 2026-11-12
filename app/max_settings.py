"""
Настройки бота MAX (`/max/`) для дублирования новостей в канал/группу
(app/max_bot.py) и уведомлений людям. Бота создаёт подтверждённая организация на
business.max.ru («Чат-боты»), токен — там же после модерации. Настраивает
только председатель, как и Telegram (app/telegram_settings.py).
"""
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from . import audit
from . import database
from . import max_bot
from . import messenger_outbox
from . import progress as progress_ui
from .auth import roles_required
from .bank_api import crypto
from .i18n import translate as _
from .max_bot import MaxError
from .models import MaxSettings, MessengerOutbox, RoleEnum

bp = Blueprint("max_settings", __name__, url_prefix="/max")

_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")


def _get_or_create_settings() -> MaxSettings:
    settings = database.db_session.query(MaxSettings).first()
    if settings is None:
        settings = MaxSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


@bp.route("/")
@roles_required(RoleEnum.CHAIRMAN)
def view():
    settings = _get_or_create_settings()
    return render_template(
        "max_settings/page.html", settings=settings,
        is_configured=max_bot.is_configured(settings),
        known_chats=max_bot.known_chats(settings),
        outbox=messenger_outbox.problems(messenger_outbox.CHANNEL_MAX),
    )


_OUTBOX_ACTIONS = {
    "retry": (messenger_outbox.retry, "max.outbox_retry", "Сообщение MAX #{id} поставлено на повторную отправку"),
    "mark-sent": (messenger_outbox.mark_sent, "max.outbox_mark_sent", "Сообщение MAX #{id} отмечено отправленным"),
    "cancel": (messenger_outbox.cancel, "max.outbox_cancel", "Отправка сообщения MAX #{id} отменена"),
}


@bp.route("/outbox/<int:item_id>/<action>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def outbox_action(item_id, action):
    if action not in _OUTBOX_ACTIONS:
        abort(404)
    item = database.db_session.get(MessengerOutbox, item_id)
    if item is None or item.channel != messenger_outbox.CHANNEL_MAX:
        abort(404)
    func, audit_action, audit_text = _OUTBOX_ACTIONS[action]
    func(item)
    audit.record(audit_action, audit_text.format(id=item.id))
    database.db_session.commit()
    flash(_("Готово."), "success")
    return redirect(url_for("max_settings.view"))


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """Пустое поле токена оставляет прежнее значение (как у Telegram)."""
    settings = _get_or_create_settings()
    f = request.form

    chat_id = f.get("news_chat_id", "").strip()
    if chat_id and not _CHAT_ID_RE.match(chat_id):
        flash(_("ID канала MAX — число (его показывает список «Чаты бота» ниже)."), "danger")
        return redirect(url_for("max_settings.view"))
    settings.news_chat_id = chat_id or None

    token = f.get("bot_token", "").strip()
    if token:
        settings.bot_token_encrypted = crypto.encrypt(token)
        settings.bot_username = None  # другой бот — имя узнаем при проверке
        settings.last_marker = None
    settings.last_error = None

    audit.record("max.settings_save", f"Настройки бота MAX обновлены (канал новостей: {settings.news_chat_id or '—'})")
    database.db_session.commit()
    flash(_("Настройки MAX сохранены."), "success")
    return redirect(url_for("max_settings.view"))


@bp.route("/test-connection", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def test_connection():
    settings = _get_or_create_settings()
    if not max_bot.is_configured(settings):
        flash(_("Сначала укажите токен бота."), "warning")
        return redirect(url_for("max_settings.view"))
    back = url_for("max_settings.view")

    def task(progress):
        settings = _get_or_create_settings()
        try:
            with progress.step(_("Бот MAX (GET /me)")) as step:
                me = max_bot.get_me(settings)
                name = me.get("username") or me.get("first_name") or "?"
                step.ok(name)
            settings.bot_username = me.get("username") or me.get("first_name")
            settings.last_error = None
            problems = []
            if settings.news_chat_id:
                with progress.step(_("Канал для новостей")) as step:
                    problems = max_bot.check_news_chat(settings)
                    if problems:
                        step.fail("; ".join(problems))
                    else:
                        step.ok(_("бот — администратор с правом публикации"))
            if problems:
                result = progress_ui.Result(False, _(
                    "Бот работает, но публиковать в канал не сможет: {problems}. Откройте канал в MAX → "
                    "Подписчики, сделайте бота администратором и разрешите ему писать.",
                    problems="; ".join(problems)), back)
            else:
                result = progress_ui.Result(True, _("Подключение работает: бот {name}.", name=name), back)
        except MaxError as exc:
            settings.last_error = str(exc)
            result = progress_ui.Result(False, _("Не удалось подключиться: {error}", error=str(exc)), back)
        database.db_session.commit()
        return result

    return progress_ui.respond(task)


@bp.route("/refresh-chats", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def refresh_chats():
    """Запросить апдейты сейчас, не дожидаясь cron, — сразу после того, как
    бота добавили в канал."""
    try:
        max_bot.poll_updates()
    except MaxError as exc:
        flash(_("Не удалось получить события бота: {error}", error=str(exc)), "danger")
    return redirect(url_for("max_settings.view"))
