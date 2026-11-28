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
    (группа правления — по MaxSettings.board_chat_id);
  * личный диалог с ботом (команды правления, app/max_commands.py) —
    тому кооперативу, где привязан аккаунт; привязан в нескольких —
    выбранному (кнопки «С каким кооперативом работаем?», команда
    «кооператив»). Нажатие кнопки — тому, кто её показал: в payload
    кнопки код кооператива («<код>:…»).

Без этих переменных окружения кооператив работает как раньше — сам себе
бот. Переменные ставит ansible (max_route_secret в vars.yml).
"""
import hmac

import requests
from flask import Blueprint, current_app, jsonify, request

from . import database
from .models import MaxUserRoute, Person

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


LOCAL = ""  # код основного кооператива в MaxUserRoute (выбор «этот»)
CHOICE = "coop|"


def _dialog_user(update: dict):
    kind = update.get("update_type")
    if kind == "message_callback":
        return ((update.get("callback") or {}).get("user") or {}).get("user_id")
    return ((update.get("message") or {}).get("sender") or {}).get("user_id")


def _targets(max_user_id, known: dict[str, str]) -> list[str]:
    codes = []
    if database.db_session.query(Person).filter_by(max_user_id=max_user_id).first() is not None:
        codes.append(LOCAL)
    for route_ in database.db_session.query(MaxUserRoute).filter_by(max_user_id=max_user_id):
        if route_.coop_code in known and route_.coop_code not in codes:
            codes.append(route_.coop_code)
    return codes


def _selected(max_user_id, codes: list[str]) -> str | None:
    row = database.db_session.query(MaxUserRoute).filter_by(max_user_id=max_user_id, selected=True).first()
    return row.coop_code if row and row.coop_code in codes else None


def select(max_user_id, code: str) -> None:
    rows = database.db_session.query(MaxUserRoute).filter_by(max_user_id=max_user_id).all()
    if not any(r.coop_code == code for r in rows):
        row = MaxUserRoute(max_user_id=max_user_id, coop_code=code)
        database.db_session.add(row)
        rows.append(row)
    for r in rows:
        r.selected = r.coop_code == code


def peer_name(code: str, url: str) -> str:
    """Название кооператива — спросить у него самого (пустая пересылка)."""
    try:
        resp = requests.post(f"{url}/max/route", json={"updates": []},
                             headers={SECRET_HEADER: _config("MAX_ROUTE_SECRET")}, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("coop_name") or code
    except (requests.RequestException, ValueError):
        pass
    return code


def _send_choice(settings, max_user_id, codes: list[str], known: dict[str, str]) -> None:
    from . import max_commands

    buttons = []
    for code in codes:
        name = max_commands._coop_name() if code == LOCAL else peer_name(code, known[code])
        buttons.append([(name, f"{CHOICE}{code or '-'}")])
    max_commands.send_plain(settings, max_user_id, "Вы в правлении нескольких кооперативов. С каким работаем?", buttons)


def _handle_choice(settings, update: dict, known: dict[str, str]) -> None:
    from . import max_commands

    callback = update.get("callback") or {}
    max_user_id = (callback.get("user") or {}).get("user_id")
    code = (callback.get("payload") or "")[len(CHOICE):]
    code = LOCAL if code == "-" else code
    if code not in _targets(max_user_id, known):
        max_commands.answer_plain(settings, callback.get("callback_id"), "Этот кооператив вам недоступен.")
        return
    select(max_user_id, code)
    name = max_commands._coop_name() if code == LOCAL else peer_name(code, known[code])
    max_commands.answer_plain(settings, callback.get("callback_id"),
                              f"Работаем с «{name}». Сменить — команда «кооператив».")


def _route_dialog(update: dict, settings, known: dict[str, str]) -> str | None:
    """Куда отдать сообщение или нажатие в личном диалоге: код кооператива
    (LOCAL — себе) или None — обработано здесь (выбор кооператива)."""
    kind = update.get("update_type")
    max_user_id = _dialog_user(update)
    if kind == "message_callback":
        payload = (update.get("callback") or {}).get("payload") or ""
        if payload.startswith(CHOICE):
            _handle_choice(settings, update, known)
            return None
        code, sep, _rest = payload.partition(":")
        return code if sep and code in known else LOCAL
    codes = _targets(max_user_id, known)
    text = " ".join((((update.get("message") or {}).get("body") or {}).get("text") or "").split()).lower()
    if len(codes) > 1 and (text in ("кооператив", "/coop") or _selected(max_user_id, codes) is None):
        _send_choice(settings, max_user_id, codes, known)
        return None
    if len(codes) > 1:
        return _selected(max_user_id, codes)
    return codes[0] if codes else LOCAL


def dispatch(updates: list[dict], settings=None) -> list[dict]:
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
        elif kind == "message_callback" or (
                kind == "message_created"
                and ((update.get("message") or {}).get("recipient") or {}).get("chat_type") == "dialog"):
            code = _route_dialog(update, settings, targets)
            if code == LOCAL:
                local.append(update)
            elif code is not None:
                outgoing[code].append(update)
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
    if not updates:  # основной спрашивает название (кнопки выбора кооператива)
        from .max_commands import _coop_name
        return jsonify(linked=[], coop_name=_coop_name())
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

