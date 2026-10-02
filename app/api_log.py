"""
Отладочный журнал обмена с внешними API (банк, eWeLink, СМС, Telegram,
контрагенты…) — instance/logs/external_api-ГГГГ-ММ-ДД.log.

Перехватывается requests.Session.send — через него идут и requests.get/post,
и сессии, так что журнал пишется для всех сервисов без правки мест вызова,
в т.ч. для будущих. На каждый запрос — строка «→» (метод, адрес, тело) и
строка «←» (статус, время, тело ответа) или «✗» (исключение: TLS, таймаут,
обрыв соединения — ответа нет вовсе).

Секреты маскируются: значения ключей вида token/secret/password/key/… в
адресе, форме и JSON (в т.ч. в ответе — Сбер отдаёт refresh_token), токен
бота Telegram в пути, заголовок Authorization не пишется вовсе. Тела
обрезаются до API_LOG_MAX_BODY символов; не текстовые — только размер.

Файл на каждый день, а не RotatingFileHandler: в него пишут несколько
воркеров gunicorn и cron-скрипты одновременно, а ротация переименованием
из нескольких процессов теряет строки. Старше API_LOG_KEEP_DAYS дней —
удаляются при старте.
"""
import datetime as dt
import glob
import json
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

logger = logging.getLogger("external_api")

_SECRET_KEY_RE = re.compile(
    r"token|secret|password|passwd|api_?key|apikey|^key$|^at$|^rt$|auth|sign|session|cookie|captcha|response$",
    re.IGNORECASE,
)
_TELEGRAM_BOT_RE = re.compile(r"/bot[^/]+/")
_TEXT_TYPES = ("json", "xml", "text", "x-www-form-urlencoded", "javascript", "html")
MASK = "***"

_original_send = None


class _DailyFileHandler(logging.Handler):
    """Пишет в <dir>/<prefix>-ГГГГ-ММ-ДД.log, открывая файл на каждую запись
    в режиме дозаписи — безопасно для нескольких процессов."""

    def __init__(self, directory: str, prefix: str):
        super().__init__()
        self.directory = directory
        self.prefix = prefix

    def emit(self, record):
        try:
            path = os.path.join(self.directory, f"{self.prefix}-{dt.date.today().isoformat()}.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


def _mask_pairs(pairs):
    return [(k, MASK if _SECRET_KEY_RE.search(k) else v) for k, v in pairs]


def mask_url(url: str) -> str:
    parts = urlsplit(url)
    path = _TELEGRAM_BOT_RE.sub(f"/bot{MASK}/", parts.path)
    query = urlencode(_mask_pairs(parse_qsl(parts.query, keep_blank_values=True)), safe="*")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _mask_json(obj):
    if isinstance(obj, dict):
        return {
            k: (MASK if isinstance(k, str) and _SECRET_KEY_RE.search(k) and not isinstance(v, (dict, list)) else _mask_json(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_json(v) for v in obj]
    return obj


def format_body(body, content_type: str | None, max_len: int) -> str:
    if body is None or body == b"" or body == "":
        return ""
    content_type = (content_type or "").lower()
    if isinstance(body, bytes):
        if content_type and not any(t in content_type for t in _TEXT_TYPES):
            return f"<{len(body)} байт, {content_type}>"
        text = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        text = body
    else:
        return f"<{type(body).__name__}>"  # поток/генератор — не читаем, иначе запрос уйдёт пустым

    stripped = text.lstrip()
    if "json" in content_type or stripped[:1] in ("{", "["):
        try:
            text = json.dumps(_mask_json(json.loads(text)), ensure_ascii=False)
        except ValueError:
            pass
    elif "x-www-form-urlencoded" in content_type:
        text = urlencode(_mask_pairs(parse_qsl(text, keep_blank_values=True)), safe="*")
    if len(text) > max_len:
        text = text[:max_len] + f"… (+{len(text) - max_len} симв.)"
    return text


def install(log_dir: str, keep_days: int = 7, max_body: int = 4000) -> None:
    """Включает журнал. Повторный вызов (второй create_app в том же
    процессе) только перенастраивает каталог/лимиты."""
    global _original_send
    os.makedirs(log_dir, exist_ok=True)
    _cleanup(log_dir, keep_days)

    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = _DailyFileHandler(log_dir, "external_api")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(process)d] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # не дублировать тела ответов в общий лог/stderr
    logger.max_body = max_body

    if _original_send is not None:
        return
    _original_send = requests.Session.send

    def send(self, request, **kwargs):
        max_len = getattr(logger, "max_body", 4000)
        url = mask_url(request.url)
        logger.info(
            "→ %s %s %s", request.method, url,
            format_body(request.body, request.headers.get("Content-Type"), max_len),
        )
        started = time.monotonic()
        try:
            resp = _original_send(self, request, **kwargs)
        except Exception as e:
            # requests повторяет в тексте ошибки адрес как есть — с секретами
            error = repr(e).replace(request.url, url).replace(request.path_url, mask_url(request.path_url))
            logger.info("✗ %s %s (%d мс): %s", request.method, url, (time.monotonic() - started) * 1000, error)
            raise
        elapsed = (time.monotonic() - started) * 1000
        if kwargs.get("stream"):
            body = "<stream>"
        else:
            body = format_body(resp.content, resp.headers.get("Content-Type"), max_len)
        logger.info("← %s %s %s (%d мс) %s", resp.status_code, request.method, url, elapsed, body)
        return resp

    requests.Session.send = send


def _cleanup(log_dir: str, keep_days: int) -> None:
    border = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    for path in glob.glob(os.path.join(log_dir, "external_api-*.log")):
        day = os.path.basename(path)[len("external_api-"):-len(".log")]
        if day < border:
            try:
                os.remove(path)
            except OSError:
                pass
