"""
Настройки Telegram-бота (`/telegram/`) для уведомлений (app/notifications.py,
app/telegram_bot.py) — токен создаётся у @BotFather, настраивает только
председатель, тот же принцип, что у почты/СМС/eWeLink/API банка
(app/mailbox.py, app/sms_settings.py, app/electricity_monitor.py, app/bank_sync.py).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from . import database
from . import progress as progress_ui
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, TelegramOutbox, TelegramSettings
from .bank_api import crypto
from . import telegram_bot
from . import telegram_outbox
from .telegram_bot import TelegramError

bp = Blueprint("telegram_settings", __name__, url_prefix="/telegram")


def _get_or_create_settings() -> TelegramSettings:
    settings = database.db_session.query(TelegramSettings).first()
    if settings is None:
        settings = TelegramSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


@bp.route("/")
@roles_required(RoleEnum.CHAIRMAN)
def view():
    settings = _get_or_create_settings()
    return render_template(
        "telegram_settings/page.html", settings=settings,
        is_configured=telegram_bot.is_configured(settings),
        outbox=telegram_outbox.problems(),
    )


_OUTBOX_ACTIONS = {
    "retry": (telegram_outbox.retry, "telegram.outbox_retry", "Сообщение Telegram #{id} поставлено на повторную отправку"),
    "mark-sent": (telegram_outbox.mark_sent, "telegram.outbox_mark_sent", "Сообщение Telegram #{id} отмечено отправленным"),
    "cancel": (telegram_outbox.cancel, "telegram.outbox_cancel", "Отправка сообщения Telegram #{id} отменена"),
}


@bp.route("/outbox/<int:item_id>/<action>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def outbox_action(item_id, action):
    if action not in _OUTBOX_ACTIONS:
        abort(404)
    item = database.db_session.get(TelegramOutbox, item_id)
    if item is None:
        abort(404)
    func, audit_action, audit_text = _OUTBOX_ACTIONS[action]
    func(item)
    audit.record(audit_action, audit_text.format(id=item.id))
    database.db_session.commit()
    flash(_("Готово."), "success")
    return redirect(url_for("telegram_settings.view"))


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """Пустое поле токена оставляет прежнее значение — тот же приём, что
    у API-ключа SMS Aero/пароля почты/client_secret банка."""
    settings = _get_or_create_settings()
    f = request.form

    settings.bot_username = f.get("bot_username", "").strip().lstrip("@") or None
    settings.news_chat_id = f.get("news_chat_id", "").strip() or None

    api_base_url = f.get("api_base_url", "").strip().rstrip("/")
    if api_base_url and not api_base_url.startswith(("https://", "http://")):
        flash(_("Адрес Bot API должен начинаться с https://"), "danger")
        return redirect(url_for("telegram_settings.view"))
    settings.api_base_url = api_base_url or None

    proxy_url = f.get("proxy_url", "").strip()
    if f.get("proxy_clear"):
        settings.proxy_url_encrypted = None
    elif proxy_url:
        if not proxy_url.startswith(telegram_bot.PROXY_SCHEMES):
            flash(_("Прокси указывается адресом вида socks5h://логин:пароль@хост:порт или http://хост:порт."), "danger")
            return redirect(url_for("telegram_settings.view"))
        settings.proxy_url_encrypted = crypto.encrypt(proxy_url)
    token = f.get("bot_token", "").strip()
    if token:
        settings.bot_token_encrypted = crypto.encrypt(token)
    settings.last_error = None

    audit.record("telegram.settings_save", f"Настройки Telegram-бота обновлены (бот: @{settings.bot_username or '—'}, группа новостей: {settings.news_chat_id or '—'}, адрес API: {settings.api_base_url or 'стандартный'}, прокси: {'задан' if settings.proxy_url_encrypted else 'нет'})")
    database.db_session.commit()
    flash(_("Настройки Telegram сохранены."), "success")
    return redirect(url_for("telegram_settings.view"))


@bp.route("/test-connection", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def test_connection():
    settings = _get_or_create_settings()
    if not telegram_bot.is_configured(settings):
        flash(_("Сначала укажите токен и имя бота."), "warning")
        return redirect(url_for("telegram_settings.view"))
    back = url_for("telegram_settings.view")

    def task(progress):
        settings = _get_or_create_settings()
        try:
            with progress.step(_("Бот Telegram (getMe)")) as step:
                me = telegram_bot.get_me(settings)
                step.ok("@" + me.get("username", "?"))
            settings.last_error = None
            result = progress_ui.Result(
                True, _("Подключение работает: бот @{username}.", username=me.get("username", "?")), back)
        except TelegramError as exc:
            settings.last_error = str(exc)
            result = progress_ui.Result(False, _("Не удалось подключиться: {error}", error=str(exc)), back)
        database.db_session.commit()
        return result

    return progress_ui.respond(task)
