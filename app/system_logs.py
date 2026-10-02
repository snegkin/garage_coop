"""
Просмотр логов сервера (`/system-logs/`) — instance/logs: cron-скрипты
(sync_bank_accounts, poll_ewelink…) и журнал внешних API (app/api_log.py),
чтобы разбираться со сбоями без захода на сервер.

Только председатель: в логах персональные данные (ФИО/ИНН плательщиков из
выписки) и подробности обмена с банком. Файл выбирается только из
фактического содержимого каталога логов — имя из адреса сверяется со
списком, так что ../ и прочие пути наружу не пройдут.

Логи бывают большими (poll_ewelink — десятки МБ), поэтому читается только
хвост файла: последние N строк, а при поиске — последние SEARCH_TAIL_BYTES.
"""
import datetime as dt
import os
import re

from flask import Blueprint, render_template, request, abort, current_app, send_file

from .auth import roles_required
from .models import RoleEnum

bp = Blueprint("system_logs", __name__, url_prefix="/system-logs")

_LOG_NAME_RE = re.compile(r"^[\w.-]+\.log(\.\d+)?$")
LINE_CHOICES = (200, 500, 2000, 10000)
DEFAULT_LINES = 500
SEARCH_TAIL_BYTES = 20 * 1024 * 1024
# Строки, которые стоит заметить: ошибки cron-скриптов, трассировки,
# сбои и 4xx/5xx-ответы в журнале внешних API.
_ERROR_RE = re.compile(
    r"ОШИБКА|\bERROR\b|\bCRITICAL\b|^Traceback \(most recent call last\)"
    r"|^[\w.]*(?:Error|Exception)\b(?::|$)"  # последняя строка трассировки: «ValueError: …»
)
_WARNING_RE = re.compile(r"\bWARNING\b|ПРЕДУПРЕЖДЕНИЕ")
# Строка журнала внешних API (app/api_log.py): «… [pid] → …», «← 200 …», «✗ …».
# Её уровень — только по статусу: в телах ответов полно слов вроде
# "hasException"/"error": 0, которые к сбою отношения не имеют.
_API_LINE_RE = re.compile(r"^\S+ \S+ \[\d+\] ([→←✗]) (\d{3})?")


def _log_dir() -> str:
    return current_app.config["LOG_FOLDER"]


def _list_logs() -> list[dict]:
    directory = _log_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    result = []
    for name in names:
        path = os.path.join(directory, name)
        if not _LOG_NAME_RE.match(name) or not os.path.isfile(path):
            continue
        st = os.stat(path)
        result.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, "modified": dt.datetime.fromtimestamp(st.st_mtime)})
    result.sort(key=lambda f: f["mtime"], reverse=True)
    return result


def _resolve(name: str) -> str:
    if name not in {f["name"] for f in _list_logs()}:
        abort(404)
    return os.path.join(_log_dir(), name)


def read_tail(path: str, max_lines: int, query: str | None = None) -> tuple[list[str], bool]:
    """Последние max_lines строк файла (с фильтром по подстроке без учёта
    регистра — среди последних SEARCH_TAIL_BYTES). Второе значение — файл
    прочитан не целиком (есть более ранние строки)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if query:
            start = max(0, size - SEARCH_TAIL_BYTES)
            f.seek(start)
            data = f.read()
        else:
            # Читаем блоками с конца, пока не наберём нужное число строк.
            block = 64 * 1024
            start = size
            data = b""
            while start > 0 and data.count(b"\n") <= max_lines:
                start = max(0, start - block)
                f.seek(start)
                data = f.read(size - start)
    lines = data.decode("utf-8", errors="replace").splitlines()
    truncated = start > 0
    if truncated and lines:
        lines = lines[1:]  # первая строка — скорее всего обрезана посередине
    if query:
        q = query.lower()
        lines = [line for line in lines if q in line.lower()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return lines, truncated


def line_level(line: str) -> str | None:
    m = _API_LINE_RE.match(line)
    if m:
        arrow, status = m.groups()
        if arrow == "✗" or (arrow == "←" and status and status[0] in "45"):
            return "error"
        return None
    if _ERROR_RE.search(line):
        return "error"
    if _WARNING_RE.search(line):
        return "warning"
    return None


@bp.route("/")
@roles_required(RoleEnum.CHAIRMAN)
def index():
    return render_template("system_logs/index.html", files=_list_logs())


@bp.route("/<name>")
@roles_required(RoleEnum.CHAIRMAN)
def view(name):
    path = _resolve(name)
    try:
        max_lines = int(request.args.get("lines", DEFAULT_LINES))
    except ValueError:
        max_lines = DEFAULT_LINES
    if max_lines not in LINE_CHOICES:
        max_lines = DEFAULT_LINES
    query = (request.args.get("q") or "").strip() or None
    only_errors = request.args.get("errors") == "1"

    lines, truncated = read_tail(path, max_lines if not only_errors else LINE_CHOICES[-1], query)
    rows = [(line, line_level(line)) for line in lines]
    if only_errors:
        rows = [r for r in rows if r[1] == "error"][-max_lines:]
    return render_template(
        "system_logs/view.html", name=name, rows=rows, truncated=truncated, size=os.path.getsize(path),
        max_lines=max_lines, line_choices=LINE_CHOICES, query=query or "", only_errors=only_errors,
        files=_list_logs(),
    )


@bp.route("/<name>/download")
@roles_required(RoleEnum.CHAIRMAN)
def download(name):
    return send_file(_resolve(name), mimetype="text/plain", as_attachment=True, download_name=name)
