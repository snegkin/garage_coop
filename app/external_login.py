"""
Вход на сайт через Яндекс ID и VK ID (/auth/external/).

Человек жмёт «Яндекс ID»/«VK ID» на форме входа, провайдер возвращает его
на callback с кодом авторизации, сайт меняет код на токен и получает
профиль: постоянный ID пользователя у провайдера и подтверждённый
провайдером номер телефона. Дальше:

1. Есть привязка (ExternalIdentity) с этим ID — вход в её учётную запись.
2. Нет — ищем Person по телефону, как при входе по телефону
   (auth._person_by_phone_digits: только однозначное совпадение). Нашли —
   заводим привязку (и учётную запись, если её ещё нет: тот же логин и
   роль, что при самостоятельной регистрации по телефону, только без
   СМС-кода — номер уже подтвердил провайдер) и входим.
3. Номера нет в базе — отказ: войти по логину и привязать аккаунт в профиле.

Вошедший пользователь может привязать аккаунт вручную из профиля (та же
кнопка, тот же callback — режим «привязка» запоминается в сессии).

Протоколы:
- Яндекс ID — OAuth 2.0 (oauth.yandex.ru), приложение типа «Для
  авторизации» с правами «Доступ к логину, имени и фамилии» и «Доступ к
  номеру телефона»; профиль — login.yandex.ru/info.
- VK ID — OAuth 2.1 с PKCE (id.vk.com), секрет приложения не нужен;
  доступ «Номер телефона» — расширенный, VK выдаёт его после верификации
  профиля бизнеса в VK Бизнес ID.

**Важно**: адреса и форматы ответов взяты из документации провайдеров и
живым входом ещё не проверены — при первой настройке смотреть журнал
внешних API (app/api_log.py), если что-то не сойдётся.
"""
import base64
import datetime as dt
import hashlib
import json
import secrets
from dataclasses import dataclass

import requests
from sqlalchemy import func
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from . import audit
from . import database
from .auth import (
    _complete_login, _login_form_choices, _normalize_phone_digits, _person_by_phone_digits,
    initial_role, is_safe_next_url, login_required, roles_required,
)
from .bank_api import crypto
from .i18n import translate as _
from .login_generation import generate_unique_login
from .models import ExternalIdentity, ExternalLoginSettings, Phone, RoleEnum, User
from .rate_limit import limiter

bp = Blueprint("external_login", __name__, url_prefix="/auth/external")

YANDEX = "yandex"
VK = "vk"
PROVIDER_TITLES = {YANDEX: "Яндекс ID", VK: "VK ID"}

YANDEX_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
YANDEX_TOKEN_URL = "https://oauth.yandex.ru/token"
YANDEX_INFO_URL = "https://login.yandex.ru/info"
YANDEX_SCOPE = "login:info login:default_phone"

VK_AUTHORIZE_URL = "https://id.vk.com/authorize"
VK_TOKEN_URL = "https://id.vk.com/oauth2/auth"
VK_USER_INFO_URL = "https://id.vk.com/oauth2/user_info"
VK_SCOPE = "phone"

TIMEOUT = 15
SESSION_KEY = "external_login"


class ExternalLoginError(Exception):
    pass


@dataclass
class Profile:
    subject: str
    phone_digits: str | None
    display_name: str | None


def get_settings() -> ExternalLoginSettings:
    settings = database.db_session.query(ExternalLoginSettings).first()
    if settings is None:
        settings = ExternalLoginSettings()
        database.db_session.add(settings)
        database.db_session.flush()
    return settings


def enabled_providers(settings: ExternalLoginSettings | None = None) -> list[str]:
    """Провайдеры, для которых заполнены реквизиты, — кнопки на форме входа
    и в профиле. Без записи настроек — пусто (не создаём её на каждой
    странице ради кнопок)."""
    if settings is None:
        settings = database.db_session.query(ExternalLoginSettings).first()
    if settings is None:
        return []
    result = []
    if settings.yandex_client_id and settings.yandex_client_secret_encrypted:
        result.append(YANDEX)
    if settings.vk_client_id:
        result.append(VK)
    return result


def callback_url(provider: str) -> str:
    return url_for("external_login.callback", provider=provider, _external=True)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _json_or_error(resp: requests.Response, what: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise ExternalLoginError(f"{what}: HTTP {resp.status_code}, ответ не JSON")
    if not isinstance(data, dict):
        raise ExternalLoginError(f"{what}: неожиданный ответ")
    if resp.status_code >= 400 or data.get("error"):
        detail = data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}"
        raise ExternalLoginError(f"{what}: {detail}")
    return data


def _post(url: str, data: dict, what: str) -> dict:
    try:
        resp = requests.post(url, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ExternalLoginError(f"{what}: {exc}")
    return _json_or_error(resp, what)


# --- Яндекс ID --------------------------------------------------------------

def yandex_authorize_url(settings: ExternalLoginSettings, redirect_uri: str, state: str) -> str:
    req = requests.Request("GET", YANDEX_AUTHORIZE_URL, params={
        "response_type": "code", "client_id": settings.yandex_client_id,
        "redirect_uri": redirect_uri, "state": state, "scope": YANDEX_SCOPE,
    })
    return req.prepare().url


def yandex_profile(settings: ExternalLoginSettings, code: str) -> Profile:
    token = _post(YANDEX_TOKEN_URL, {
        "grant_type": "authorization_code", "code": code,
        "client_id": settings.yandex_client_id,
        "client_secret": crypto.decrypt(settings.yandex_client_secret_encrypted),
    }, "Яндекс ID, получение токена").get("access_token")
    if not token:
        raise ExternalLoginError("Яндекс ID не выдал токен доступа")
    try:
        resp = requests.get(
            YANDEX_INFO_URL, params={"format": "json"},
            headers={"Authorization": f"OAuth {token}"}, timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ExternalLoginError(f"Яндекс ID, данные пользователя: {exc}")
    info = _json_or_error(resp, "Яндекс ID, данные пользователя")
    if not info.get("id"):
        raise ExternalLoginError("Яндекс ID не вернул ID пользователя")
    phone = (info.get("default_phone") or {}).get("number")
    return Profile(
        subject=str(info["id"]),
        phone_digits=_normalize_phone_digits(phone) if phone else None,
        display_name=info.get("login") or info.get("display_name") or info.get("real_name"),
    )


# --- VK ID ------------------------------------------------------------------

def vk_authorize_url(settings: ExternalLoginSettings, redirect_uri: str, state: str, challenge: str) -> str:
    req = requests.Request("GET", VK_AUTHORIZE_URL, params={
        "response_type": "code", "client_id": settings.vk_client_id,
        "redirect_uri": redirect_uri, "state": state, "scope": VK_SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return req.prepare().url


def vk_profile(settings: ExternalLoginSettings, code: str, device_id: str, state: str,
               verifier: str, redirect_uri: str) -> Profile:
    token = _post(VK_TOKEN_URL, {
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": settings.vk_client_id, "device_id": device_id,
        "redirect_uri": redirect_uri, "state": state,
    }, "VK ID, получение токена").get("access_token")
    if not token:
        raise ExternalLoginError("VK ID не выдал токен доступа")
    data = _post(VK_USER_INFO_URL, {
        "client_id": settings.vk_client_id, "access_token": token,
    }, "VK ID, данные пользователя")
    user = data.get("user") or {}
    if not user.get("user_id"):
        raise ExternalLoginError("VK ID не вернул ID пользователя")
    phone = user.get("phone")
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return Profile(
        subject=str(user["user_id"]),
        phone_digits=_normalize_phone_digits(str(phone)) if phone else None,
        display_name=name or None,
    )


def _vk_callback_params(args) -> dict:
    """VK возвращает code/device_id/state параметрами адреса; в части
    сценариев — одним JSON в параметре payload. Понимаем оба."""
    if args.get("payload"):
        try:
            payload = json.loads(args["payload"])
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            return {k: str(v) for k, v in payload.items() if v is not None}
    return {k: args.get(k) for k in ("code", "device_id", "state", "error", "error_description")}


# --- Маршруты ---------------------------------------------------------------

def _check_provider(provider: str) -> ExternalLoginSettings:
    if provider not in PROVIDER_TITLES:
        abort(404)
    settings = database.db_session.query(ExternalLoginSettings).first()
    if provider not in enabled_providers(settings):
        abort(404)
    return settings


# Коды ошибки в адресе возврата, когда человек сам отказался (нажал
# «Отмена» или не дал доступ) — это не сбой, в журнал не пишем.
CANCEL_ERRORS = {"access_denied", "user_denied", "cancel", "cancelled"}


def _audit_failure(detail: str) -> None:
    """Технические подробности сбоя — в журнал, человеку на экран — общее
    сообщение. Пишется и при привязке из профиля (тогда актор — g.user)."""
    audit.record("auth.login_failed", summary=f"Не удалось войти через внешний сервис: {detail}")
    database.db_session.commit()


def _persons_by_phone_digits(digits: str) -> set[int]:
    """ID всех карточек с этим номером — чтобы отличить «номера нет» от
    «номер в нескольких карточках» (auth._person_by_phone_digits в обоих
    случаях отдаёт None)."""
    return {
        p.person_id for p in database.db_session.query(Phone)
        if _normalize_phone_digits(p.number) == digits
    }


def _mask_phone(digits: str) -> str:
    return f"…{digits[-4:]}" if len(digits) >= 4 else digits


@bp.route("/<provider>/start", methods=["POST"])
@limiter.limit("20 per minute")
def start(provider):
    """Кнопка на форме входа (с галочками формы) или в профиле (привязка —
    если человек уже вошёл)."""
    settings = _check_provider(provider)
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    next_url = request.args.get("next")
    session[SESSION_KEY] = {
        "provider": provider, "state": state, "verifier": verifier,
        "link": g.user is not None,
        "next": next_url if is_safe_next_url(next_url) else None,
        **_login_form_choices(),
    }
    redirect_uri = callback_url(provider)
    if provider == YANDEX:
        return redirect(yandex_authorize_url(settings, redirect_uri, state))
    return redirect(vk_authorize_url(settings, redirect_uri, state, challenge))


@bp.route("/<provider>/callback")
@limiter.limit("20 per minute")
def callback(provider):
    settings = _check_provider(provider)
    pending = session.pop(SESSION_KEY, None)
    title = PROVIDER_TITLES[provider]
    params = _vk_callback_params(request.args) if provider == VK else request.args
    fail_to = url_for("cabinet.profile") if g.user is not None else url_for("auth.login")

    if not params.get("code"):
        error = params.get("error") or ""
        if error in CANCEL_ERRORS:
            flash(_("Вход через {provider} отменён.", provider=title), "info")
        else:
            detail = params.get("error_description") or error or "нет кода авторизации"
            _audit_failure(f"{title} вернул ошибку: {detail}")
            flash(_("{provider} не выполнил вход — попробуйте ещё раз или войдите по логину.", provider=title), "danger")
        return redirect(fail_to)
    if not pending or pending.get("provider") != provider or params.get("state") != pending.get("state"):
        flash(_("Не удалось подтвердить запрос входа — попробуйте ещё раз."), "danger")
        return redirect(fail_to)

    try:
        if provider == YANDEX:
            profile = yandex_profile(settings, params["code"])
        else:
            profile = vk_profile(
                settings, params["code"], params.get("device_id") or "", params["state"],
                pending["verifier"], callback_url(provider),
            )
    except ExternalLoginError as exc:
        _audit_failure(str(exc))
        flash(_("Не удалось связаться с {provider} — попробуйте позже или войдите по логину.", provider=title), "danger")
        return redirect(fail_to)

    if pending.get("link") and g.user is not None:
        return _link(provider, profile)
    return _login(provider, profile, pending)


def _link(provider: str, profile: Profile):
    title = PROVIDER_TITLES[provider]
    back = redirect(url_for("cabinet.profile"))
    existing = database.db_session.query(ExternalIdentity).filter_by(provider=provider, subject=profile.subject).first()
    if existing is not None:
        if existing.user_id == g.user.id:
            flash(_("Этот аккаунт {provider} уже привязан.", provider=title), "info")
        else:
            flash(_("Этот аккаунт {provider} уже привязан к другой учётной записи.", provider=title), "danger")
        return back
    if database.db_session.query(ExternalIdentity).filter_by(provider=provider, user_id=g.user.id).first():
        flash(_("К учётной записи уже привязан другой аккаунт {provider} — сначала отвяжите его.", provider=title), "danger")
        return back
    database.db_session.add(ExternalIdentity(
        user_id=g.user.id, provider=provider, subject=profile.subject, display_name=profile.display_name,
    ))
    audit.record(
        "account.external_link", entity_type="user", entity_id=g.user.id,
        summary=f"К учётной записи «{g.user.username}» привязан {title} ({profile.display_name or profile.subject})",
    )
    database.db_session.commit()
    flash(_("{provider} привязан — теперь можно входить через него.", provider=title), "success")
    return back


def _login(provider: str, profile: Profile, pending: dict):
    title = PROVIDER_TITLES[provider]
    login_page = redirect(url_for("auth.login"))
    identity = database.db_session.query(ExternalIdentity).filter_by(provider=provider, subject=profile.subject).first()
    how = f"через {title}"

    if identity is not None:
        user = identity.user
    else:
        if not profile.phone_digits:
            audit.record("auth.login_failed", summary=f"Неудачная попытка входа {how}: провайдер не передал телефон")
            database.db_session.commit()
            flash(_("{provider} не передал номер телефона. Войдите по логину или телефону и привяжите {provider} в профиле.", provider=title), "danger")
            return login_page
        person_ids = _persons_by_phone_digits(profile.phone_digits)
        if len(person_ids) > 1:
            audit.record(
                "auth.login_failed",
                summary=f"Неудачная попытка входа {how}: телефон {_mask_phone(profile.phone_digits)} указан в нескольких карточках ({len(person_ids)})",
            )
            database.db_session.commit()
            flash(_("Номер телефона из {provider} указан сразу в нескольких карточках кооператива, поэтому войти по нему нельзя. Сообщите правлению — карточки нужно исправить.", provider=title), "danger")
            return login_page
        person = _person_by_phone_digits(profile.phone_digits)
        if person is None:
            audit.record(
                "auth.login_failed",
                summary=f"Неудачная попытка входа {how}: телефон {_mask_phone(profile.phone_digits)} не найден в базе",
            )
            database.db_session.commit()
            flash(_("Номер телефона из {provider} не найден в базе кооператива. Войдите по логину или телефону и привяжите {provider} в профиле, либо сообщите правлению свой номер.", provider=title), "danger")
            return login_page
        user = database.db_session.query(User).filter_by(person_id=person.id).first()
        if user is not None and database.db_session.query(ExternalIdentity).filter_by(provider=provider, user_id=user.id).first():
            audit.record(
                "auth.login_failed", entity_type="user", entity_id=user.id, actor=user,
                summary=f"Неудачная попытка входа {how}: к «{user.username}» привязан другой аккаунт",
            )
            database.db_session.commit()
            flash(_("К вашей учётной записи привязан другой аккаунт {provider}. Войдите через него или по логину.", provider=title), "danger")
            return login_page
        if user is None:
            existing_usernames = {u for (u,) in database.db_session.query(User.username)}
            username = generate_unique_login(person.full_name, existing_usernames)
            user = User(
                username=username, password_hash=generate_password_hash(secrets.token_urlsafe(32)),
                role=initial_role(person), person_id=person.id, is_active=True,
            )
            database.db_session.add(user)
            database.db_session.flush()
            audit.record(
                "account.self_register_external", entity_type="user", entity_id=user.id,
                summary=f"Учётная запись «{username}» создана самостоятельно {how} для {person.full_name} (телефон подтверждён провайдером)",
            )
        identity = ExternalIdentity(
            user_id=user.id, provider=provider, subject=profile.subject, display_name=profile.display_name,
        )
        database.db_session.add(identity)
        audit.record(
            "account.external_link", entity_type="user", entity_id=user.id, actor=user,
            summary=f"К учётной записи «{user.username}» привязан {title} по совпадению телефона",
        )

    if not user.is_active:
        audit.record(
            "auth.login_failed", entity_type="user", entity_id=user.id, actor=user,
            summary=f"Попытка входа {how} в отключённую учётную запись «{user.username}»",
        )
        database.db_session.commit()
        flash(_("Учётная запись отключена."), "danger")
        return login_page

    identity.last_login_at = dt.datetime.utcnow()
    if profile.display_name:
        identity.display_name = profile.display_name
    return _complete_login(
        user, f"Успешный вход {how}: «{user.username}»",
        remember=bool(pending.get("remember")), enable_push=bool(pending.get("enable_push")),
        next_url=pending.get("next"),
    )


@bp.route("/<provider>/unlink", methods=["POST"])
@login_required
def unlink(provider):
    if provider not in PROVIDER_TITLES:
        abort(404)
    identity = database.db_session.query(ExternalIdentity).filter_by(provider=provider, user_id=g.user.id).first()
    if identity is not None:
        title = PROVIDER_TITLES[provider]
        database.db_session.delete(identity)
        audit.record(
            "account.external_unlink", entity_type="user", entity_id=g.user.id,
            summary=f"От учётной записи «{g.user.username}» отвязан {title}",
        )
        database.db_session.commit()
        flash(_("{provider} отвязан.", provider=title), "success")
    return redirect(url_for("cabinet.profile"))


def identities_for(user: User) -> dict[str, ExternalIdentity]:
    return {
        i.provider: i
        for i in database.db_session.query(ExternalIdentity).filter_by(user_id=user.id)
    }


# --- Настройки (председатель) -----------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def settings_page():
    """Пустое поле секрета оставляет прежнее значение (как у токенов ботов)."""
    settings = get_settings()
    if request.method == "POST":
        f = request.form
        settings.yandex_client_id = f.get("yandex_client_id", "").strip() or None
        secret = f.get("yandex_client_secret", "").strip()
        if secret:
            settings.yandex_client_secret_encrypted = crypto.encrypt(secret)
        if not settings.yandex_client_id:
            settings.yandex_client_secret_encrypted = None
        vk_client_id = f.get("vk_client_id", "").strip()
        if vk_client_id and not vk_client_id.isdigit():
            flash(_("ID приложения VK — число."), "danger")
            return redirect(url_for("external_login.settings_page"))
        settings.vk_client_id = vk_client_id or None
        enabled = ", ".join(PROVIDER_TITLES[p] for p in enabled_providers(settings)) or "никакой"
        audit.record("external_login.settings_save", f"Настройки входа через Яндекс/VK обновлены (включены: {enabled})")
        database.db_session.commit()
        flash(_("Настройки входа сохранены."), "success")
        return redirect(url_for("external_login.settings_page"))

    linked = dict(
        database.db_session.query(ExternalIdentity.provider, func.count())
        .group_by(ExternalIdentity.provider).all()
    )
    database.db_session.commit()
    return render_template(
        "external_login/settings.html", settings=settings,
        enabled=enabled_providers(settings), linked=linked,
        yandex_callback=callback_url(YANDEX), vk_callback=callback_url(VK),
    )



@bp.app_template_global()
def external_login_links() -> list[dict]:
    """Для профиля (external_login/_profile.html): настроенные провайдеры и
    привязки текущего пользователя. Отвязать можно и выключенный провайдер."""
    if g.get("user") is None:
        return []
    identities = identities_for(g.user)
    enabled = enabled_providers()
    return [
        {"provider": p, "title": title, "identity": identities.get(p), "enabled": p in enabled}
        for p, title in PROVIDER_TITLES.items()
        if p in enabled or p in identities
    ]
