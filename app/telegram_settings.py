"""
Настройки Telegram-бота (`/telegram/`) для уведомлений (app/notifications.py,
app/telegram_bot.py) — токен создаётся у @BotFather, настраивает только
председатель, тот же принцип, что у почты/СМС/eWeLink/API банка
(app/mailbox.py, app/sms_settings.py, app/electricity_monitor.py, app/bank_sync.py).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, TelegramSettings
from .bank_api import crypto
from . import telegram_bot
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
    )


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """Пустое поле токена оставляет прежнее значение — тот же приём, что
    у API-ключа SMS Aero/пароля почты/client_secret банка."""
    settings = _get_or_create_settings()
    f = request.form

    settings.bot_username = f.get("bot_username", "").strip().lstrip("@") or None
    token = f.get("bot_token", "").strip()
    if token:
        settings.bot_token_encrypted = crypto.encrypt(token)
    settings.last_error = None

    audit.record("telegram.settings_save", f"Настройки Telegram-бота обновлены (бот: @{settings.bot_username or '—'})")
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
    try:
        me = telegram_bot.get_me(settings)
        settings.last_error = None
        flash(_("Подключение работает: бот @{username}.", username=me.get("username", "?")), "success")
    except TelegramError as exc:
        settings.last_error = str(exc)
        flash(_("Не удалось подключиться: {error}", error=str(exc)), "danger")
    database.db_session.commit()
    return redirect(url_for("telegram_settings.view"))
