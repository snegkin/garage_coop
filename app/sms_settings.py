"""
Настройки СМС (`/sms/`) — используется для подтверждения номера телефона
при самостоятельной регистрации и для восстановления пароля по телефону
(см. app/auth.py, app/verification.py, app/sms/). Настраивает только
председатель — тот же принцип, что у почты/eWeLink/API банка
(app/mailbox.py, app/electricity_monitor.py, app/bank_sync.py).

Сама страница НЕ хранит email/API-ключ — только ссылку на то, какой
контрагент (раздел «Контрагенты») сейчас обслуживает отправку
(SmsSettings.counterparty_id, см. app/models.py). Реквизиты вводятся на
карточке этого контрагента, кнопкой «Настроить API» (app/counterparties.py)
— тот же приём, что и у Beget/будущих провайдеров, чтобы не было разных
мест настройки для разных контрагентов.
"""
import datetime as dt
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, SmsSettings, Counterparty, CounterpartyApiProvider, SmsLog, Cooperative
from .sms import get_sms_client, SmsError, sms_site_identifier, SMS_CAPABLE_PROVIDERS

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
    """
    Одна страница — журнал отправленных СМС (см. models.SmsLog, app/sms/
    __init__.py: _LoggingSmsClient) сразу, настройки провайдера и
    тестовая отправка — в модальном окне по кнопке «Настройки» (раньше
    были отдельными страницами /sms/ и /sms/log — объединены, чтобы не
    прыгать между ними при разборе жалоб «SMS не приходят»: сразу видно
    и журнал, и куда нажать, если дело в самих настройках).
    """
    settings = _get_or_create_settings()
    entries = (
        database.db_session.query(SmsLog)
        .order_by(SmsLog.sent_at.desc(), SmsLog.id.desc())
        .limit(500)
        .all()
    )
    eligible_counterparties = (
        database.db_session.query(Counterparty)
        .filter(Counterparty.api_provider.in_(SMS_CAPABLE_PROVIDERS))
        .order_by(Counterparty.name)
        .all()
    )
    return render_template(
        "sms_settings/page.html", settings=settings,
        eligible_counterparties=eligible_counterparties,
        is_configured=get_sms_client() is not None,
        entries=entries,
    )


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """Сами email/api_key здесь больше не задаются — только выбор
    контрагента, который их предоставляет (см. докстринг модуля).
    Реквизиты настраиваются на карточке контрагента."""
    settings = _get_or_create_settings()
    counterparty_id = request.form.get("counterparty_id", "").strip()
    settings.counterparty_id = int(counterparty_id) if counterparty_id else None

    database.db_session.commit()
    flash(_("Настройки СМС сохранены."), "success")
    return redirect(url_for("sms_settings.view"))


@bp.route("/test", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def send_test():
    settings = _get_or_create_settings()
    client = get_sms_client()
    if client is None:
        flash(_("Сначала выберите контрагента и настройте его API — см. раздел «Контрагенты»."), "warning")
        return redirect(url_for("sms_settings.view"))

    test_phone = request.form.get("test_phone", "").strip()
    digits = _normalize_test_phone(test_phone)
    if len(digits) < 7:
        flash(_("Укажите корректный номер телефона для теста."), "warning")
        return redirect(url_for("sms_settings.view"))

    coop = database.db_session.query(Cooperative).first()
    site = sms_site_identifier(coop)
    # Название/сайт кооператива в тексте — то же требование SMS Aero, что
    # и у кода подтверждения (см. auth._sms_code_text): "система учёта
    # кооператива" сама по себе — общая фраза, одинаковая у всех
    # инсталляций, не конкретное название компании/сайта.
    test_text = _("Тестовое сообщение из системы учёта кооператива ({site}).", site=site) if site \
        else _("Тестовое сообщение из системы учёта кооператива.")
    try:
        client.send(digits, test_text)
        settings.last_test_result = _("Успешно отправлено.")
    except SmsError as exc:
        settings.last_test_result = str(exc)
        audit.record("sms.test_failed", summary=f"Ошибка тестовой отправки СМС: {exc}")
    settings.last_test_at = dt.datetime.utcnow()
    database.db_session.commit()
    return redirect(url_for("sms_settings.view"))
