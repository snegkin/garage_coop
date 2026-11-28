"""
Один бот MAX на все кооперативы сервера.

Бота MAX создаёт только подтверждённая организация (business.max.ru), а
юрлицо есть не у каждого кооператива — поэтому бот один (зарегистрирован
на основной кооператив), а кооперативов на сервере несколько. У одного
токена может быть только один читатель апдейтов (GET /updates с маркером
забирает их), поэтому:

  * основной кооператив опрашивает MAX как обычно (scripts/poll_messengers.py)
    и раздаёт апдейты: dispatch() оставляет себе свои, остальным пересылает
    POST /max/route на их внутренний адрес (MAX_ROUTE_PEERS) с общим
    секретом (MAX_ROUTE_SECRET);
  * остальные (MAX_RECEIVE=forwarded) MAX не опрашивают, только отправляют
    (уведомления, новости в свой канал) тем же токеном и принимают
    пересланное в route();
  * диплинк привязки аккаунта у них с меткой кооператива:
    ?start=<MAX_COOP_CODE>__<токен> — по ней основной понимает, кому
    передать bot_started. Ответ «привязал» основной запоминает в
    MaxUserRoute (пригодится, чтобы направлять диалог с ботом в нужный
    кооператив);
  * события групп и каналов (бота добавили/убрали, сообщения в группе)
    получают все: каждый кооператив сам решает, его ли это группа
    (группа правления — по MaxSettings.board_chat_id).

Без этих переменных окружения кооператив работает как раньше — сам себе
бот. Переменные ставит ansible (max_route_secret в vars.yml).
"""
import hmac

import requests
from flask import Blueprint, current_app, jsonify, request

from . import database
from .models import MaxUserRoute

bp = Blueprint("max_route", __name__)

SEPARATOR = "__"
FORWARD_TIMEOUT = 30
SECRET_HEADER = "X-Max-Route-Secret"


def _config(name: str, default: str = "") -> str:
    return (current_app.config.get(name) or default).strip()


def coop_code() -> str:
    return _config("MAX_COOP_CODE")


def receives_forwarded() -> bool:
    """Этот кооператив MAX не опрашивает — апдейты ему пересылает основной."""
    return _config("MAX_RECEIVE") == "forwarded"


def peers() -> dict[str, str]:
    """MAX_ROUTE_PEERS: «код=http://127.0.0.1:8001,код2=…» -> {код: адрес}."""
    result = {}
    for item in _config("MAX_ROUTE_PEERS").split(","):
        code, sep, url = item.strip().partition("=")
        if sep and code.strip() and url.strip():
            result[code.strip()] = url.strip().rstrip("/")
    return result


def link_payload(token: str) -> str:
    """Что положить в ?start= диплинка привязки: у пересылаемого кооператива —
    с меткой, чтобы основной знал, кому отдать."""
    code = coop_code()
    return f"{code}{SEPARATOR}{token}" if receives_forwarded() and code else token


def strip_payload(payload: str | None) -> str:
    """Токен привязки без метки кооператива (своя метка или её нет)."""
    text = (payload or "").strip()
    code, sep, token = text.rpartition(SEPARATOR)
    if sep and code == coop_code():
        return token
    return text


def _payload_code(update: dict) -> str | None:
    code, sep, _token = (update.get("payload") or "").strip().rpartition(SEPARATOR)
    return code if sep else None


def _forward(code: str, url: str, updates: list[dict]) -> list:
    """Переслать апдейты кооперативу; -> user_id, кого он привязал."""
    if not updates:
        return []
    try:
        resp = requests.post(
            f"{url}/max/route", json={"updates": updates},
            headers={SECRET_HEADER: _config("MAX_ROUTE_SECRET")}, timeout=FORWARD_TIMEOUT,
        )
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        return resp.json().get("linked") or []
    except (requests.RequestException, ValueError) as exc:
        # Апдейты уже забраны у MAX — повторить нечем; привязку человек
        # повторит сам (ссылка в профиле), события групп — не критичны.
        current_app.logger.error("MAX: не удалось переслать %d апдейт(ов) кооперативу %s: %s",
                                 len(updates), code, exc)
        return []


def remember(max_user_id, code: str) -> None:
    if max_user_id is None:
        return
    exists = database.db_session.query(MaxUserRoute).filter_by(max_user_id=max_user_id, coop_code=code).first()
    if exists is None:
        database.db_session.add(MaxUserRoute(max_user_id=max_user_id, coop_code=code))


def dispatch(updates: list[dict]) -> list[dict]:
    """Основной кооператив: разослать чужие апдейты, вернуть свои.
    Без MAX_ROUTE_PEERS — все свои, как и было."""
    targets = peers()
    if not targets:
        return updates
    local = []
    outgoing: dict[str, list[dict]] = {code: [] for code in targets}
    for update in updates:
        kind = update.get("update_type")
        if kind == "bot_started":
            code = _payload_code(update)
            if code in targets:
                outgoing[code].append(update)
            else:
                local.append(update)
        elif kind == "message_created" and ((update.get("message") or {}).get("recipient") or {}).get("chat_type") == "dialog":
            # Личный диалог с ботом — пока только свой (команды бота — отдельно).
            local.append(update)
        else:
            # Группы и каналы — всем: каждый сам знает свои.
            local.append(update)
            for code in targets:
                outgoing[code].append(update)
    for code, items in outgoing.items():
        for user_id in _forward(code, targets[code], items):
            remember(user_id, code)
    return local


@bp.route("/max/route", methods=["POST"])
def route():
    """Приём апдейтов от основного кооператива (только с общим секретом)."""
    from . import max_bot

    secret = _config("MAX_ROUTE_SECRET")
    given = request.headers.get(SECRET_HEADER, "")
    if not secret or not receives_forwarded() or not hmac.compare_digest(secret, given):
        return jsonify(error="forbidden"), 403
    data = request.get_json(silent=True) or {}
    updates = data.get("updates")
    if not isinstance(updates, list):
        return jsonify(error="bad request"), 400
    settings = max_bot.get_settings()
    if not max_bot.is_configured(settings):
        current_app.logger.warning("MAX: пришли апдейты от основного кооператива, а токен бота здесь не задан")
        return jsonify(linked=[], error="bot not configured"), 200
    linked = max_bot.handle_updates(settings, [u for u in updates if isinstance(u, dict)])
    database.db_session.commit()
    return jsonify(linked=[u for u in linked if u is not None])


def describe() -> str | None:
    """Для страницы настроек MAX: как этот кооператив получает апдейты."""
    if receives_forwarded():
        return "forwarded"
    if peers():
        return "router"
    return None

