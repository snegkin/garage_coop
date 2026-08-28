"""
Страница мониторинга текущей мощности/напряжения/тока по 3 фазам вводного
щита с устройств Sonoff POWCT (облако eWeLink) — см. app/ewelink/ (клиент),
app/models.py (EWeLinkAccount/PowerPhaseDevice/PowerPhaseReading),
scripts/poll_ewelink.py (фоновый опрос раз в минуту по cron, как и
scripts/update_key_rate.py для ставки ЦБ РФ).

Это отдельная подсистема от app/power.py (помесячный биллинг по общему
счётчику) — здесь только снимки для мониторинга, без начислений.

Права: просмотр — правление (BOARD, как и app/power.py), настройки
подключения к eWeLink и привязка устройств к фазам — только председатель
(CHAIRMAN), по аналогии с настройками API банка в app/bank_sync.py.
"""
import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import database
from .i18n import translate as _
from .auth import roles_required
from .models import RoleEnum, EWeLinkAccount, PowerPhaseDevice, PowerPhaseReading
from .bank_api import crypto
from .ewelink import EWeLinkClient, EWeLinkTokens, EWeLinkApiError, EWeLinkAuthError

bp = Blueprint("electricity_monitor", __name__, url_prefix="/electricity")

HISTORY_HOURS = 3  # глубина истории на графике/в таблице — намеренно небольшая: раздел для текущего мониторинга, не для архива


def _get_or_create_account() -> EWeLinkAccount:
    account = database.db_session.query(EWeLinkAccount).first()
    if account is None:
        account = EWeLinkAccount()
        database.db_session.add(account)
        database.db_session.flush()
    return account


def build_client(account: EWeLinkAccount) -> EWeLinkClient | None:
    """None, если подключение ещё не настроено (нет app_id/секрета/логина/пароля) —
    вызывающий код (view() ниже, scripts/poll_ewelink.py) должен показать
    понятное сообщение вместо ошибки, как и bank_api.get_client()."""
    if not (account.app_id and account.app_secret_encrypted and account.email and account.password_encrypted):
        return None

    tokens = None
    if account.access_token_encrypted and account.refresh_token_encrypted:
        tokens = EWeLinkTokens(
            access_token=crypto.decrypt(account.access_token_encrypted),
            refresh_token=crypto.decrypt(account.refresh_token_encrypted),
            region=account.region or "eu",
            obtained_at=account.token_obtained_at.timestamp() if account.token_obtained_at else 0.0,
        )

    return EWeLinkClient(
        app_id=account.app_id,
        app_secret=crypto.decrypt(account.app_secret_encrypted),
        email=account.email,
        password=crypto.decrypt(account.password_encrypted),
        tokens=tokens,
        region=account.region or "eu",
    )


def persist_tokens(account: EWeLinkAccount, client: EWeLinkClient) -> None:
    """Сохраняет токены клиента в account (без commit — вызывающий код коммитит сам).
    Вызывать сразу после login()/refresh(), даже если следующий запрос упадёт —
    тот же принцип, что и для Sberbank (см. app/bank_sync.py:_persist_rotated_refresh_token).
    Без подчёркивания (в отличие от bank_sync.py) — эту функцию, в отличие от
    того аналога, использует не только сам модуль, но и внешний скрипт
    scripts/poll_ewelink.py, которому нужна публичная точка входа."""
    if not client.tokens:
        return
    account.access_token_encrypted = crypto.encrypt(client.tokens.access_token)
    account.refresh_token_encrypted = crypto.encrypt(client.tokens.refresh_token)
    account.region = client.tokens.region
    account.token_obtained_at = dt.datetime.utcnow()


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def view():
    account = _get_or_create_account()
    devices = (
        database.db_session.query(PowerPhaseDevice)
        .order_by(PowerPhaseDevice.sort_order, PowerPhaseDevice.id)
        .all()
    )

    since = dt.datetime.utcnow() - dt.timedelta(hours=HISTORY_HOURS)
    latest_by_device = {}
    history_by_device = {}
    for device in devices:
        latest_by_device[device.id] = (
            database.db_session.query(PowerPhaseReading)
            .filter_by(device_id=device.id)
            .order_by(PowerPhaseReading.ts.desc())
            .first()
        )
        history_by_device[device.id] = (
            database.db_session.query(PowerPhaseReading)
            .filter(PowerPhaseReading.device_id == device.id, PowerPhaseReading.ts >= since)
            .order_by(PowerPhaseReading.ts.desc())
            .limit(50)
            .all()
        )

    power_values = [r.power_w for r in latest_by_device.values() if r and r.power_w is not None]
    total_power = sum(power_values) if power_values else None

    return render_template(
        "electricity/monitor.html",
        account=account,
        devices=devices,
        latest_by_device=latest_by_device,
        history_by_device=history_by_device,
        total_power=total_power,
        is_configured=bool(account.app_id and account.email),
    )


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """
    Пароль/appSecret шифруются перед сохранением. Пустое поле в форме
    оставляет прежнее значение (чтобы не заставлять председателя заново
    вводить пароль при каждой правке app_id, например) — как и в
    app/bank_sync.py:save_api_settings для client_secret.
    Смена логина/пароля/appId сбрасывает сохранённые токены — со старыми
    учётными данными они всё равно больше не действительны.
    """
    account = _get_or_create_account()
    f = request.form

    app_id = f.get("app_id", "").strip()
    app_secret = f.get("app_secret", "").strip()
    email = f.get("email", "").strip()
    password = f.get("password", "").strip()

    credentials_changed = (
        app_id != (account.app_id or "")
        or email != (account.email or "")
        or bool(app_secret)
        or bool(password)
    )

    account.app_id = app_id or None
    account.email = email or None
    if app_secret:
        account.app_secret_encrypted = crypto.encrypt(app_secret)
    if password:
        account.password_encrypted = crypto.encrypt(password)

    if credentials_changed:
        account.access_token_encrypted = None
        account.refresh_token_encrypted = None
        account.token_obtained_at = None
        account.last_error = None

    database.db_session.commit()
    flash(_("Настройки подключения к eWeLink сохранены."), "success")
    return redirect(url_for("electricity_monitor.view"))


@bp.route("/devices", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_devices():
    """
    Сохраняет привязку ровно 3 устройств к фазам A/B/C одной формой (проще
    для председателя, чем добавлять/редактировать по одному) — если
    устройство с таким id фазы уже есть, обновляет его deviceid/label,
    иначе создаёт. Пустой deviceid у фазы просто пропускается (например,
    третье устройство ещё не куплено/не подключено).
    """
    f = request.form
    labels = {"a": _("Фаза A"), "b": _("Фаза B"), "c": _("Фаза C")}
    for i, phase in enumerate(("a", "b", "c")):
        device_id = f.get(f"device_id_{phase}", "").strip()
        if not device_id:
            continue
        existing = (
            database.db_session.query(PowerPhaseDevice)
            .filter_by(sort_order=i)
            .first()
        )
        if existing is None:
            existing = PowerPhaseDevice(sort_order=i, label=labels[phase])
            database.db_session.add(existing)
        existing.label = f.get(f"label_{phase}", "").strip() or labels[phase]
        existing.ewelink_device_id = device_id
        existing.is_active = True

    database.db_session.commit()
    flash(_("Устройства сохранены."), "success")
    return redirect(url_for("electricity_monitor.view"))


@bp.route("/test-connection", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def test_connection():
    """Ручная проверка подключения прямо со страницы — логинится (или
    обновляет токен, если он уже есть) и один раз запрашивает список
    устройств, не дожидаясь следующего запуска cron-поллера."""
    account = _get_or_create_account()
    client = build_client(account)
    if client is None:
        flash(_("Сначала заполните все поля подключения к eWeLink."), "warning")
        return redirect(url_for("electricity_monitor.view"))

    try:
        if client.tokens is None:
            client.login()
        else:
            try:
                client.list_devices()
            except EWeLinkAuthError:
                client.refresh()
                client.list_devices()
        persist_tokens(account, client)
        account.last_error = None
        account.last_poll_at = dt.datetime.utcnow()
        flash(_("Подключение к eWeLink работает."), "success")
    except EWeLinkApiError as exc:
        account.last_error = str(exc)
        flash(_("Ошибка подключения к eWeLink: {error}", error=str(exc)), "danger")

    database.db_session.commit()
    return redirect(url_for("electricity_monitor.view"))
