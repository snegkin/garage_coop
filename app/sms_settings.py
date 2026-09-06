"""
Настройки СМС-провайдера (`/sms/`) — используется для подтверждения
номера телефона при самостоятельной регистрации и для восстановления
пароля по телефону (см. app/auth.py, app/verification.py, app/sms/).
Настраивает только председатель — тот же принцип, что у почты/eWeLink/
API банка (app/mailbox.py, app/electricity_monitor.py, app/bank_sync.py).

Сейчас реализован только провайдер SMS Aero (models.SmsProvider) — поле
provider уже есть в модели на случай добавления второго агрегатора позже
(см. app/sms/__init__.py:get_sms_client).
"""
import datetime as dt
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, SmsSettings, SmsProvider
from .bank_api import crypto
from .sms import get_sms_client, SmsError

bp = Blueprint("sms_settings", __name__, url_prefix="/sms")

_PHONE_NON_DIGIT_RE = re.compile(r"\D")


def _normalize_test_phone(raw: str) -> str:
    """Тот же принцип, что auth._normalize_phone_digits — не импортируем
    напрямую приватную функцию из другого модуля ради одного места
    использования (тестовая отправка ниже), тривиальная логика проще
    продублировать."""
    digits = _PHONE_NON_DIGIT_RE.sub("", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return digits


def _get_or_create_settings() -> SmsSettings:
    settings = database.db_session.query(SmsSettings).first()
    if settings is None:
        settings = SmsSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


@bp.route("/")
@roles_required(RoleEnum.CHAIRMAN)
def view():
    settings = _get_or_create_settings()
    return render_template(
        "sms_settings/form.html", settings=settings,
        is_configured=get_sms_client(settings) is not None,
    )


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """Пустое поле api_key оставляет прежнее значение (не заставляем
    председателя вводить его заново при каждой правке email/подписи) —
    тот же приём, что и у App Secret eWeLink/client_secret банка."""
    settings = _get_or_create_settings()
    f = request.form

    settings.provider = SmsProvider.SMSAERO
    settings.smsaero_email = f.get("smsaero_email", "").strip() or None
    api_key = f.get("smsaero_api_key", "").strip()
    if api_key:
        settings.smsaero_api_key_encrypted = crypto.encrypt(api_key)
    settings.sender_sign = f.get("sender_sign", "").strip() or None

    database.db_session.commit()
    flash(_("Настройки СМС сохранены."), "success")
    return redirect(url_for("sms_settings.view"))


@bp.route("/test", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def send_test():
    settings = _get_or_create_settings()
    client = get_sms_client(settings)
    if client is None:
        flash(_("Сначала укажите email и API-ключ SMS Aero."), "warning")
        return redirect(url_for("sms_settings.view"))

    test_phone = request.form.get("test_phone", "").strip()
    digits = _normalize_test_phone(test_phone)
    if len(digits) < 7:
        flash(_("Укажите корректный номер телефона для теста."), "warning")
        return redirect(url_for("sms_settings.view"))

    try:
        client.send(digits, _("Тестовое сообщение из системы учёта кооператива."))
        settings.last_test_result = _("Успешно отправлено.")
    except SmsError as exc:
        settings.last_test_result = str(exc)
        audit.record("sms.test_failed", summary=f"Ошибка тестовой отправки СМС: {exc}")
    settings.last_test_at = dt.datetime.utcnow()
    database.db_session.commit()
    return redirect(url_for("sms_settings.view"))
