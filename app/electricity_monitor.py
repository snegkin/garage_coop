"""
Страница мониторинга текущей мощности/напряжения/тока по 3 фазам вводного
щита с устройств Sonoff POWCT (облако eWeLink) — см. app/ewelink/ (клиент),
app/models.py (EWeLinkAccount/PowerPhaseDevice/PowerPhaseReading),
scripts/poll_ewelink.py (фоновый опрос раз в минуту по cron, как и
scripts/update_key_rate.py для ставки ЦБ РФ).

Это отдельная подсистема от app/power.py (помесячный биллинг по общему
счётчику) — здесь только снимки для мониторинга, без начислений.

Права: просмотр — любой вошедший член кооператива (по прямой просьбе —
данные о текущем энергопотреблении не чувствительны); настройки
подключения к eWeLink и привязка устройств к фазам — только председатель
(CHAIRMAN), по аналогии с настройками API банка в app/bank_sync.py.
Шаблон (electricity/monitor.html) уже гейтит все админ-элементы через
is_chairman(), а не is_board() — открытие view()/history_data() всем
не расширяет доступ ни к чему настроечному.

Авторизация в eWeLink — официальный OAuth2 Open API (authorization code
flow, приложение зарегистрировано на dev.ewelink.cc), см. app/ewelink/client.py.
Председатель проходит три шага: 1) сохраняет App ID/App Secret приложения
(save_settings), 2) жмёт «Войти через eWeLink» (start_oauth) — браузер
уходит на страницу авторизации eWeLink и возвращается на oauth_callback с
кодом, 3) выбирает дом (family) — без family_id список устройств не
получить (save_family).
"""
import datetime as dt
import secrets

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from sqlalchemy import func

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import RoleEnum, EWeLinkAccount, PowerPhaseDevice, PowerPhaseReading
from .bank_api import crypto
from .ewelink import EWeLinkClient, EWeLinkTokens, EWeLinkApiError, EWeLinkAuthError

bp = Blueprint("electricity_monitor", __name__, url_prefix="/electricity")

HISTORY_HOURS = 24 * 7  # период по умолчанию для графика при первом открытии страницы (см. view())
MAX_CHART_POINTS = 1500  # точек на устройство в ответе history_data() — при 30 днях с опросом раз в 5 минут сырых точек ~8600, а за более старую историю может быть больше; без ограничения график будет тормозить
OAUTH_STATE_SESSION_KEY = "ewelink_oauth_state"


def _parse_iso_utc(value: str | None) -> dt.datetime | None:
    """Разбирает ISO-таймстамп от JS (Date#toISOString(), всегда UTC с суффиксом
    "Z") в наивный UTC datetime — тот же формат, что хранится в PowerPhaseReading.ts
    (см. models.py). None при отсутствии/некорректном значении — вызывающий код
    сам решает, что подставить по умолчанию."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _downsample(rows: list, max_points: int) -> list:
    """Прореживает ряд точек до max_points элементов равномерным шагом по
    индексу (не усреднением) — для визуального графика мониторинга этого
    достаточно, а усреднение по интервалам добавило бы отдельную логику
    ради вспомогательного эндпоинта, который не участвует в биллинге."""
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    return [rows[int(i * step)] for i in range(max_points)]


def _get_or_create_account() -> EWeLinkAccount:
    account = database.db_session.query(EWeLinkAccount).first()
    if account is None:
        account = EWeLinkAccount()
        database.db_session.add(account)
        database.db_session.flush()
    return account


def build_client(account: EWeLinkAccount) -> EWeLinkClient | None:
    """None, если подключение ещё не настроено (нет app_id/секрета) —
    вызывающий код (view() ниже, scripts/poll_ewelink.py) должен показать
    понятное сообщение вместо ошибки, как и bank_api.get_client(). Клиент
    без токенов (до прохождения authorize_url/exchange_code) валиден —
    им можно построить только authorize_url()."""
    if not (account.app_id and account.app_secret_encrypted):
        return None

    app_secret = crypto.decrypt(account.app_secret_encrypted)
    if not app_secret:
        # decrypt() вернул None — расшифровать не удалось (например,
        # SECRET_KEY/BANK_API_ENCRYPTION_KEY с тех пор сменился). Без секрета
        # клиент неработоспособен (authorize_url/_sign используют его сразу,
        # без токена) — тот же случай, что и «не настроено».
        return None

    tokens = None
    if account.access_token_encrypted and account.refresh_token_encrypted:
        access_token = crypto.decrypt(account.access_token_encrypted)
        refresh_token = crypto.decrypt(account.refresh_token_encrypted)
        # decrypt() возвращает None, если расшифровать не удалось (например,
        # SECRET_KEY/BANK_API_ENCRYPTION_KEY отличается между веб-процессом и
        # cron-скриптом poll_ewelink.py) — в этом случае считаем, что токенов
        # нет, а не строим EWeLinkTokens с access_token=None: иначе клиент
        # молча отправит в eWeLink заголовок "Bearer None" вместо понятной
        # ошибки "нужна повторная авторизация".
        if access_token and refresh_token:
            tokens = EWeLinkTokens(
                access_token=access_token,
                refresh_token=refresh_token,
                region=account.region or "eu",
                obtained_at=account.token_obtained_at.timestamp() if account.token_obtained_at else 0.0,
            )

    return EWeLinkClient(
        app_id=account.app_id,
        app_secret=app_secret,
        tokens=tokens,
        region=account.region or "eu",
    )


def persist_tokens(account: EWeLinkAccount, client: EWeLinkClient) -> None:
    """Сохраняет токены клиента в account (без commit — вызывающий код коммитит сам).
    Вызывать сразу после exchange_code()/refresh(), даже если следующий запрос упадёт —
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


def _callback_redirect_uri() -> str:
    return url_for("electricity_monitor.oauth_callback", _external=True)


@bp.route("/")
@login_required
def view():
    account = _get_or_create_account()
    devices = (
        database.db_session.query(PowerPhaseDevice)
        .order_by(PowerPhaseDevice.sort_order, PowerPhaseDevice.id)
        .all()
    )

    latest_by_device = {}
    for device in devices:
        latest_by_device[device.id] = (
            database.db_session.query(PowerPhaseReading)
            .filter_by(device_id=device.id)
            .order_by(PowerPhaseReading.ts.desc())
            .first()
        )

    power_values = [r.power_w for r in latest_by_device.values() if r and r.power_w is not None]
    total_power = sum(power_values) if power_values else None

    day_kwh_values = [r.day_kwh for r in latest_by_device.values() if r and r.day_kwh is not None]
    month_kwh_values = [r.month_kwh for r in latest_by_device.values() if r and r.month_kwh is not None]
    total_day_kwh = sum(day_kwh_values) if day_kwh_values else None
    total_month_kwh = sum(month_kwh_values) if month_kwh_values else None

    # Дом (family) ещё не выбран, но токен уже есть — предложить выбор
    # прямо на странице, не отдельным шагом мастера. Живой запрос только в
    # этом переходном состоянии (между авторизацией и выбором family_id),
    # не на каждый обычный визит на страницу.
    families = []
    families_error = None
    if account.access_token_encrypted and not account.family_id:
        client = build_client(account)
        if client is not None:
            try:
                families = client.list_families()
            except EWeLinkAuthError:
                try:
                    client.refresh()
                    persist_tokens(account, client)
                    database.db_session.commit()
                    families = client.list_families()
                except EWeLinkApiError as exc:
                    families_error = str(exc)
            except EWeLinkApiError as exc:
                families_error = str(exc)

    devices_json = [{"id": d.id, "label": d.label} for d in devices]

    # Границы для датапикеров «с/по» на графике истории (см. monitor.html) —
    # самая ранняя и самая поздняя запись по всем устройствам сразу, не по
    # каждому отдельно: один общий диапазон на весь график проще для
    # пользователя, чем разные пределы на разных фазах.
    history_min, history_max = database.db_session.query(
        func.min(PowerPhaseReading.ts), func.max(PowerPhaseReading.ts)
    ).one()

    return render_template(
        "electricity/monitor.html",
        account=account,
        devices=devices,
        devices_json=devices_json,
        latest_by_device=latest_by_device,
        total_power=total_power,
        total_day_kwh=total_day_kwh,
        total_month_kwh=total_month_kwh,
        history_hours_default=HISTORY_HOURS,
        history_min=history_min,
        history_max=history_max,
        is_configured=bool(account.app_id and account.family_id),
        is_authorized=bool(account.access_token_encrypted),
        families=families,
        families_error=families_error,
        callback_redirect_uri=_callback_redirect_uri(),
    )


@bp.route("/history-data")
@login_required
def history_data():
    """JSON для графика истории на странице (см. monitor.html): произвольный
    период — параметрами ?since=&until= (ISO UTC, см. _parse_iso_utc), задаётся
    пользователем через два datetime-local в браузере, ограниченные реальными
    границами данных (см. view():history_min/history_max). Без параметров —
    последние HISTORY_HOURS часов, тот же диапазон, что при первом открытии
    страницы. Отдаёт мощность в Вт, как она хранится в БД, — перевод в кВт и
    выбор видимых фаз/масштаб/прокрутка по времени делает JS на клиенте, без
    повторных запросов к серверу (кроме смены диапазона дат)."""
    until = _parse_iso_utc(request.args.get("until")) or dt.datetime.utcnow()
    since = _parse_iso_utc(request.args.get("since")) or (until - dt.timedelta(hours=HISTORY_HOURS))
    if since > until:
        since, until = until, since

    devices = (
        database.db_session.query(PowerPhaseDevice)
        .order_by(PowerPhaseDevice.sort_order, PowerPhaseDevice.id)
        .all()
    )

    result = []
    for device in devices:
        rows = (
            database.db_session.query(PowerPhaseReading.ts, PowerPhaseReading.power_w)
            .filter(
                PowerPhaseReading.device_id == device.id,
                PowerPhaseReading.ts >= since,
                PowerPhaseReading.ts <= until,
                PowerPhaseReading.power_w.isnot(None),
            )
            .order_by(PowerPhaseReading.ts.asc())
            .all()
        )
        rows = _downsample(rows, MAX_CHART_POINTS)
        result.append({
            "id": device.id,
            "label": device.label,
            # "Z" — ts в БД наивный UTC (см. models.py); суффикс делает JS-Date() на
            # клиенте однозначным (иначе браузер трактует его как локальное время).
            "points": [{"t": ts.isoformat() + "Z", "w": float(w)} for ts, w in rows],
        })

    return jsonify(devices=result)


@bp.route("/settings", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_settings():
    """
    App Secret шифруется перед сохранением. Пустое поле в форме оставляет
    прежнее значение (чтобы не заставлять председателя заново вводить его
    при каждой правке App ID, например) — как и в
    app/bank_sync.py:save_api_settings для client_secret.
    Смена App ID/App Secret сбрасывает сохранённые токены и family_id — со
    старым приложением dev.ewelink.cc они всё равно больше не действительны.
    """
    account = _get_or_create_account()
    f = request.form

    app_id = f.get("app_id", "").strip()
    app_secret = f.get("app_secret", "").strip()

    credentials_changed = (
        app_id != (account.app_id or "")
        or bool(app_secret)
    )

    account.app_id = app_id or None
    if app_secret:
        account.app_secret_encrypted = crypto.encrypt(app_secret)

    if credentials_changed:
        account.access_token_encrypted = None
        account.refresh_token_encrypted = None
        account.token_obtained_at = None
        account.family_id = None
        account.last_error = None

    database.db_session.commit()
    flash(_("Настройки подключения к eWeLink сохранены."), "success")
    return redirect(url_for("electricity_monitor.view"))


@bp.route("/ewelink/authorize")
@roles_required(RoleEnum.CHAIRMAN)
def start_oauth():
    """Перенаправляет председателя на страницу авторизации eWeLink. State —
    случайная строка в сессии, сверяется в oauth_callback — защита от
    CSRF/подмены чужого кода авторизации сторонним сайтом."""
    account = _get_or_create_account()
    client = build_client(account)
    if client is None:
        flash(_("Сначала укажите App ID и App Secret приложения eWeLink."), "warning")
        return redirect(url_for("electricity_monitor.view"))

    state = secrets.token_urlsafe(24)
    session[OAUTH_STATE_SESSION_KEY] = state
    return redirect(client.authorize_url(_callback_redirect_uri(), state))


@bp.route("/ewelink/callback")
@roles_required(RoleEnum.CHAIRMAN)
def oauth_callback():
    """Точка возврата из браузерной авторизации eWeLink — см. предупреждение
    в app/ewelink/client.py о том, что точный формат этой страницы/её
    редиректа не подтверждён живым тестом. Этот URL (полностью, со схемой
    и доменом) нужно прописать как redirect URI в настройках приложения на
    dev.ewelink.cc — он выводится на странице мониторинга."""
    expected_state = session.pop(OAUTH_STATE_SESSION_KEY, None)
    state = request.args.get("state")
    code = request.args.get("code")
    region = request.args.get("region")

    if not code:
        flash(_("eWeLink не вернул код авторизации: {error}", error=request.args.get("error") or "—"), "danger")
        return redirect(url_for("electricity_monitor.view"))
    if not expected_state or state != expected_state:
        flash(_("Не удалось подтвердить запрос авторизации (state не совпадает) — попробуйте ещё раз."), "danger")
        return redirect(url_for("electricity_monitor.view"))

    account = _get_or_create_account()
    client = build_client(account)
    if client is None:
        flash(_("Сначала укажите App ID и App Secret приложения eWeLink."), "warning")
        return redirect(url_for("electricity_monitor.view"))
    if region:
        client.region = region

    try:
        client.exchange_code(code, _callback_redirect_uri())
        persist_tokens(account, client)
        account.family_id = None  # новая авторизация — попросим выбрать дом заново
        account.last_error = None
        database.db_session.commit()
        flash(_("Авторизация в eWeLink прошла успешно. Осталось выбрать дом (family) ниже."), "success")
    except EWeLinkApiError as exc:
        account.last_error = str(exc)
        database.db_session.commit()
        flash(_("Ошибка авторизации в eWeLink: {error}", error=str(exc)), "danger")

    return redirect(url_for("electricity_monitor.view"))


@bp.route("/ewelink/family", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def save_family():
    account = _get_or_create_account()
    family_id = request.form.get("family_id", "").strip()
    if not family_id:
        flash(_("Выберите дом (family) из списка."), "warning")
        return redirect(url_for("electricity_monitor.view"))
    account.family_id = family_id
    database.db_session.commit()
    flash(_("Дом выбран. Теперь привяжите устройства к фазам ниже."), "success")
    return redirect(url_for("electricity_monitor.view"))


@bp.route("/ewelink/unbind", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def unbind():
    """Полный сброс подключения — отвязывает приложение от аккаунта eWeLink
    (DELETE /v2/user/oauth/token) и очищает токены/family_id локально.
    App ID/App Secret не трогает — обычно их менять не нужно, если
    председатель просто хочет переавторизоваться на том же приложении."""
    account = _get_or_create_account()
    client = build_client(account)
    if client is not None and client.tokens is not None:
        try:
            client.unbind()
        except EWeLinkApiError as exc:
            flash(_("Не удалось отвязать приложение на стороне eWeLink: {error}", error=str(exc)), "warning")

    account.access_token_encrypted = None
    account.refresh_token_encrypted = None
    account.token_obtained_at = None
    account.family_id = None
    account.last_error = None
    database.db_session.commit()
    flash(_("Подключение к eWeLink сброшено."), "success")
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
    """Ручная проверка подключения прямо со страницы — обновляет токен, если
    нужно, и один раз запрашивает список устройств выбранного дома, не
    дожидаясь следующего запуска cron-поллера."""
    account = _get_or_create_account()
    client = build_client(account)
    if client is None:
        flash(_("Сначала укажите App ID и App Secret приложения eWeLink."), "warning")
        return redirect(url_for("electricity_monitor.view"))
    if client.tokens is None:
        flash(_("Сначала пройдите авторизацию — кнопка «Войти через eWeLink»."), "warning")
        return redirect(url_for("electricity_monitor.view"))
    if not account.family_id:
        flash(_("Сначала выберите дом (family) ниже."), "warning")
        return redirect(url_for("electricity_monitor.view"))

    try:
        try:
            client.list_devices(account.family_id)
        except EWeLinkAuthError:
            client.refresh()
            persist_tokens(account, client)
            client.list_devices(account.family_id)
        persist_tokens(account, client)
        account.last_error = None
        account.last_poll_at = dt.datetime.utcnow()
        flash(_("Подключение к eWeLink работает."), "success")
    except EWeLinkApiError as exc:
        account.last_error = str(exc)
        flash(_("Ошибка подключения к eWeLink: {error}", error=str(exc)), "danger")

    database.db_session.commit()
    return redirect(url_for("electricity_monitor.view"))
