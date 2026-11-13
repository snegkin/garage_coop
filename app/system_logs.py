"""
Просмотр логов сервера (`/logs/<раздел>/`) — instance/logs: cron-скрипты
(sync_bank_accounts, poll_ewelink…) и журнал внешних API (app/api_log.py),
чтобы разбираться со сбоями без захода на сервер.

Общего списка всех логов нет — каждый лог открывается из раздела, к
которому относится (SECTIONS: выписка — из «Расчётных счетов», опрос
eWeLink — из мониторинга электроэнергии и т.д., кнопка «Логи» на странице
раздела). Журнал внешних API в разделе показывается только по адресам
его сервисов (api_hosts: банк — modulbank/sberbank…) — запись целиком,
вместе с многострочным телом ответа. Всё, что ни к одному разделу не
относится (резервные копии, обслуживание, ошибки приложения, новый
незнакомый cron-скрипт), и журнал API без фильтра — в разделе «Сервер».

Только председатель: в логах персональные данные (ФИО/ИНН плательщиков из
выписки) и подробности обмена с банком. Файл выбирается только из
фактического содержимого каталога логов — имя из адреса сверяется со
списком, так что ../ и прочие пути наружу не пройдут.

Логи бывают большими (poll_ewelink — десятки МБ), поэтому читается только
хвост файла: последние N строк, а при поиске — последние SEARCH_TAIL_BYTES.
Сжатые архивы (*.gz — rotate_log в scripts/_common.sh, дневные журналы в
app/api_log.py) с конца не прочитать — они читаются потоком целиком, но в
памяти держатся только последние N подходящих строк.
"""
import datetime as dt
import gzip
import os
import re
from collections import deque

from flask import Blueprint, render_template, request, abort, current_app, send_file

from .auth import roles_required
from .models import RoleEnum

bp = Blueprint("system_logs", __name__, url_prefix="/logs")

# code → label (заголовок), back (страница раздела, откуда пришли; None —
# раздел и есть сама страница логов), logs (имена логов без .log/.N/.gz),
# api_hosts (подстроки адресов в журнале внешних API; пусто — журнал API
# в разделе не показывается). Порядок важен только для «Сервера» —
# он последний и забирает всё, что не разобрали остальные.
SERVER_SECTION = "server"
API_LOG = "external_api"
SECTIONS = {
    "bank": {
        "label": "Расчётные счета", "back": "finance.bank_accounts",
        "logs": ("sync_bank_accounts",), "api_hosts": ("modulbank.ru", "sberbank.ru"),
    },
    "electricity": {
        "label": "Мониторинг электроэнергии", "back": "electricity_monitor.view",
        "logs": ("poll_ewelink",), "api_hosts": ("coolkit.cc", "coolkit.cn", "ewelink"),
    },
    "surveillance": {
        "label": "Видеонаблюдение", "back": "surveillance.view",
        "logs": ("dvr_snapshot",), "api_hosts": (),
    },
    "penalty": {
        "label": "Пеня и ключевая ставка", "back": "penalty.view",
        "logs": ("accrue_penalty", "update_key_rate"), "api_hosts": ("cbr.ru",),
    },
    "counterparties": {
        "label": "Контрагенты", "back": "counterparties.list_counterparties",
        "logs": ("sync_counterparty_balances",), "api_hosts": ("beget.com", "tns-e.ru", "smsaero.ru"),
    },
    "mailbox": {
        "label": "Почта правления", "back": "mailbox.inbox",
        "logs": ("poll_mailbox", "backup_mailbox"), "api_hosts": (),
    },
    "messaging": {
        "label": "Каналы уведомлений (СМС, Telegram, MAX)", "back": "sms_settings.view",
        "logs": ("poll_messengers", "reconcile_sms_charges"),
        "api_hosts": ("smsaero.ru", "api.telegram.org", "max.ru"),
    },
    SERVER_SECTION: {
        "label": "Сервер", "back": None,
        "logs": ("backup_db", "backup_offsite", "backup_pull", "repair_db", "cleanup_orphan_attachments",
                 "shrink_uploads", "board_chat_digest", "app", API_LOG),
        "api_hosts": (),
    },
}
_LOG_BASE_RE = re.compile(r"^(.+?)(?:-\d{4}-\d{2}-\d{2})?\.log(?:\.\d+)?(?:\.gz)?$")
# Начало записи журнала (и API, и logging): «2026-11-12 14:39:43,066 …».
# Строки без него — продолжение тела предыдущей записи.
_ENTRY_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")

_LOG_NAME_RE = re.compile(r"^[\w.-]+\.log(\.\d+)?(\.gz)?$")
LINE_CHOICES = (200, 500, 2000, 10000)
DEFAULT_LINES = 500
SEARCH_TAIL_BYTES = 20 * 1024 * 1024
# Строки, которые стоит заметить: ошибки cron-скриптов, трассировки,
# сбои и 4xx/5xx-ответы в журнале внешних API.
# Сообщения cron-скриптов пишутся как попало: «— ОШИБКА: …» (sync_bank_accounts),
# «Ошибка подключения к eWeLink: …», «ffmpeg завершился с кодом 145: Error
# opening…» (dvr_snapshot), «не удалось построить кадр», итог «ошибок: 6».
_ERROR_RE = re.compile(
    r"[Оо]шибк[аи]\b|ОШИБК[АИ]\b|[Нн]е удалось|\b(?:[Ee]rror|ERROR)\b|\bCRITICAL\b"
    r"|^Traceback \(most recent call last\)"
    r"|ошибок: [1-9]"
    r"|^[\w.]*(?:Error|Exception)\b(?::|$)"  # последняя строка трассировки: «ValueError: …»
)
_WARNING_RE = re.compile(r"\bWARNING\b|ПРЕДУПРЕЖДЕНИЕ")
# Без этих подстрок (в нижнем регистре) ни _ERROR_RE, ни _WARNING_RE не
# сработают. Проверка `in` в разы быстрее регулярок, а фильтр «только ошибки»
# прогоняет через line_level весь 20-МБ хвост (~250 тыс. строк poll_ewelink).
_LEVEL_HINTS = ("ошибк", "ошибок", "не удалось", "error", "critical", "traceback", "exception",
                "warning", "предупреждение")
# Служебные INFO-строки Alembic, которые до починки migrations/env.py
# писал каждый запуск cron-скрипта («setup plugin…», «Context impl…») —
# в старых логах их большинство; по умолчанию скрываются.
_ALEMBIC_NOISE_RE = re.compile(r"INFO\s+\[?alembic\.runtime\.")
# Строка журнала внешних API (app/api_log.py): «… [pid] → …», «← 200 …», «✗ …».
# Её уровень — только по статусу: в телах ответов полно слов вроде
# "hasException"/"error": 0, которые к сбою отношения не имеют.
_API_LINE_RE = re.compile(r"^\S+ \S+ \[\d+\] ([→←✗]) (\d{3})?")


def _log_dir() -> str:
    return current_app.config["LOG_FOLDER"]


def log_base(name: str) -> str:
    """sync_bank_accounts.log.2.gz → sync_bank_accounts,
    external_api-2026-11-12.log → external_api."""
    m = _LOG_BASE_RE.match(name)
    return m.group(1) if m else name


def section_of(name: str) -> list[str]:
    """Разделы, в которых виден файл: журнал API — в каждом разделе с
    api_hosts и в «Сервере»; прочее — в своём, а незнакомое — в «Сервере»."""
    base = log_base(name)
    if base == API_LOG:
        return [code for code, sec in SECTIONS.items() if sec["api_hosts"]] + [SERVER_SECTION]
    for code, sec in SECTIONS.items():
        if base in sec["logs"]:
            return [code]
    return [SERVER_SECTION]


def _host_filter(hosts):
    """keep(line) для read_tail: запись журнала API к одному из hosts —
    с её строками продолжения (многострочное тело ответа). Состояние —
    между вызовами: read_tail проверяет строки строго по порядку."""
    state = {"keep": False}

    def keep(line: str) -> bool:
        if _ENTRY_START_RE.match(line):
            lowered = line.lower()
            state["keep"] = any(h in lowered for h in hosts)
        return state["keep"]

    return keep


def _get_section(code: str) -> dict:
    if code not in SECTIONS:
        abort(404)
    return SECTIONS[code]


def _list_logs(section: str | None = None) -> list[dict]:
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
        if section is not None and section not in section_of(name):
            continue
        st = os.stat(path)
        result.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, "modified": dt.datetime.fromtimestamp(st.st_mtime)})
    result.sort(key=lambda f: f["mtime"], reverse=True)
    return result


def _resolve(section: str, name: str) -> str:
    if name not in {f["name"] for f in _list_logs(section)}:
        abort(404)
    return os.path.join(_log_dir(), name)


def read_tail(path: str, max_lines: int, query: str | None = None, keep=None) -> tuple[list[str], bool]:
    """Последние max_lines строк файла (с фильтром по подстроке без учёта
    регистра и/или функцией keep(line) — среди последних SEARCH_TAIL_BYTES).
    Второе значение — файл прочитан не целиком (есть более ранние строки)."""
    if path.endswith(".gz"):
        return _read_tail_gzip(path, max_lines, query, keep)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if query or keep:
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
    # keep — раньше подстроки: фильтр журнала API по адресу (_host_filter)
    # должен увидеть каждую строку, иначе не поймёт, чьё продолжение дальше.
    if keep:
        lines = [line for line in lines if keep(line)]
    if query:
        q = query.lower()
        lines = [line for line in lines if q in line.lower()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return lines, truncated


def _read_tail_gzip(path: str, max_lines: int, query: str | None, keep) -> tuple[list[str], bool]:
    q = query.lower() if query else None
    tail = deque(maxlen=max_lines)
    matched = 0
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if keep and not keep(line):
                continue
            if q and q not in line.lower():
                continue
            matched += 1
            tail.append(line)
    return list(tail), matched > max_lines


def line_level(line: str) -> str | None:
    m = _API_LINE_RE.match(line)
    if m:
        arrow, status = m.groups()
        if arrow == "✗" or (arrow == "←" and status and status[0] in "45"):
            return "error"
        return None
    lowered = line.lower()
    if not any(hint in lowered for hint in _LEVEL_HINTS):
        return None
    if _ERROR_RE.search(line):
        return "error"
    if _WARNING_RE.search(line):
        return "warning"
    return None


@bp.route("/<section>/")
@roles_required(RoleEnum.CHAIRMAN)
def index(section):
    sec = _get_section(section)
    return render_template(
        "system_logs/index.html", section=section, sec=sec, files=_list_logs(section),
    )


@bp.route("/<section>/<name>")
@roles_required(RoleEnum.CHAIRMAN)
def view(section, name):
    sec = _get_section(section)
    path = _resolve(section, name)
    try:
        max_lines = int(request.args.get("lines", DEFAULT_LINES))
    except ValueError:
        max_lines = DEFAULT_LINES
    if max_lines not in LINE_CHOICES:
        max_lines = DEFAULT_LINES
    query = (request.args.get("q") or "").strip() or None
    only_errors = request.args.get("errors") == "1"
    show_noise = request.args.get("noise") == "1"

    # Журнал API в разделе — только записи к его сервисам (см. SECTIONS).
    host_keep = _host_filter(sec["api_hosts"]) if log_base(name) == API_LOG and sec["api_hosts"] else None
    # Ошибки редки (раз в сутки среди поминутных «ошибок: 0») — ищем их, как и
    # подстроку, во всём хвосте SEARCH_TAIL_BYTES, а не в последних N строках.
    error_keep = (lambda line: line_level(line) == "error") if only_errors else None
    if host_keep and error_keep:
        keep = lambda line: host_keep(line) and error_keep(line)  # noqa: E731 — host_keep первым: ему нужна каждая строка
    else:
        keep = host_keep or error_keep
    # Шум выкидывается после чтения — читаем с запасом, чтобы осталось max_lines.
    lines, truncated = read_tail(path, max_lines if show_noise else LINE_CHOICES[-1], query, keep)
    if not show_noise:
        lines = [line for line in lines if not _ALEMBIC_NOISE_RE.search(line)][-max_lines:]
    rows = [(line, line_level(line)) for line in lines]
    return render_template(
        "system_logs/view.html", section=section, sec=sec, name=name, rows=rows, truncated=truncated,
        size=os.path.getsize(path), host_filtered=bool(host_keep),
        max_lines=max_lines, line_choices=LINE_CHOICES, query=query or "", only_errors=only_errors,
        show_noise=show_noise,
        files=_list_logs(section),
    )


@bp.route("/<section>/<name>/download")
@roles_required(RoleEnum.CHAIRMAN)
def download(section, name):
    _get_section(section)
    mimetype = "application/gzip" if name.endswith(".gz") else "text/plain"
    return send_file(_resolve(section, name), mimetype=mimetype, as_attachment=True, download_name=name)
