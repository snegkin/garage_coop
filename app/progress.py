"""
Окно прогресса для долгих операций с внешними API (выписка банка, eWeLink,
Telegram, СМС, ставка ЦБ…): вместо «зависшей» страницы — шаги с галочками
по мере выполнения и пояснение, на чём споткнулось.

Операция пишется как обычная функция и отмечает крупные шаги:

    def sync_something(obj_id, progress=progress.NULL):
        with progress.step("Запрос в банк") as step:
            data = client.fetch()          # HTTP-запросы внутри шага
            step.ok(f"получено {len(data)}")   # сами видны в окне строками
        ...

Каждый запрос через requests (Session.send — как и в app/api_log.py)
внутри шага показывается вложенной строкой: метод, адрес без параметров,
статус и время или ошибка (таймаут, TLS). cron и прочие вызовы без окна
передают progress.NULL — шаги ничего не делают.

Роут отдаёт результат через respond(task):
  - форма с атрибутом data-progress (base.html) шлёт Accept:
    application/x-ndjson — задача выполняется в рабочем потоке (своя сессия
    БД, тот же g.user и язык), события идут в браузер строками JSON по мере
    появления; раз в 15 с — пустой ping, чтобы nginx (proxy_read_timeout)
    не оборвал долгий запрос. Если окно закрыли — задача всё равно доходит
    до конца;
  - без JS — задача выполняется сразу, итог во flash и redirect, как было.

task(progress) возвращает Result(ok, message, redirect, category). Объекты
БД задача загружает сама по id: сессия рабочего потока — своя.
"""
from __future__ import annotations

import dataclasses
import json
import queue
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests
from flask import (Response, copy_current_request_context, flash, g, redirect, request,
                   stream_with_context)

NDJSON = "application/x-ndjson"
PING_INTERVAL = 15  # с; меньше proxy_read_timeout nginx (120 с)

OK, WARN, FAIL, SKIP, RUNNING = "ok", "warn", "fail", "skip", "running"


@dataclasses.dataclass
class Result:
    ok: bool
    message: str
    redirect: str
    # Категория flash для режима без JS; по умолчанию — по ok.
    category: str | None = None

    def flash_category(self) -> str:
        return self.category or ("success" if self.ok else "danger")


class StepFailed(Exception):
    """Поднять внутри step(): шаг — fail с этим текстом и подсказкой, а
    исключение уходит дальше, как обычное."""

    def __init__(self, detail: str, hint: str = ""):
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


class _Step:
    def __init__(self, progress: "Progress", step_id: str, title: str):
        self.progress = progress
        self.id = step_id
        self.title = title
        self.status: str | None = None
        self.detail = ""
        self.hint = ""

    def ok(self, detail: str = "") -> None:
        self._set(OK, detail, "")

    def warn(self, detail: str, hint: str = "") -> None:
        self._set(WARN, detail, hint)

    def fail(self, detail: str, hint: str = "") -> None:
        self._set(FAIL, detail, hint)

    def _set(self, status: str, detail: str, hint: str) -> None:
        self.status, self.detail, self.hint = status, detail, hint


class Progress:
    """Шаги одной операции. emit — куда отдавать события (None — никуда)."""

    def __init__(self, emit=None):
        self._emit = emit
        self._counter = 0
        self._current: _Step | None = None

    @property
    def active(self) -> bool:
        return self._emit is not None

    def event(self, **fields) -> None:
        if self._emit is not None:
            self._emit({"type": "step", **fields})

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    @contextmanager
    def step(self, title: str):
        """Шаг: running при входе; при выходе — то, что задали step.ok/warn/
        fail, иначе ok. Исключение внутри — fail с его текстом, исключение
        летит дальше."""
        step = _Step(self, self._new_id("s"), title)
        parent, self._current = self._current, step
        self.event(id=step.id, title=title, status=RUNNING, detail="", hint="")
        try:
            yield step
        except StepFailed as exc:
            step.fail(exc.detail, exc.hint)
            raise
        except Exception as exc:
            if step.status is None:
                step.fail(_exc_text(exc))
            raise
        finally:
            self._current = parent
            self.event(id=step.id, title=title, status=step.status or OK, detail=step.detail, hint=step.hint)

    def http_started(self, method: str, url: str) -> str | None:
        if self._emit is None:
            return None
        sub_id = self._new_id("h")
        self.event(id=sub_id, parent=self._current.id if self._current else None, sub=True,
                   title=f"{method} {_display_url(url)}", status=RUNNING, detail="", hint="")
        return sub_id

    def http_finished(self, sub_id: str | None, method: str, url: str, status: str, detail: str) -> None:
        if sub_id is None:
            return
        self.event(id=sub_id, parent=self._current.id if self._current else None, sub=True,
                   title=f"{method} {_display_url(url)}", status=status, detail=detail, hint="")


NULL = Progress(None)


def _exc_text(exc: Exception) -> str:
    from .api_log import mask_secrets_in_text
    text = " ".join(mask_secrets_in_text(str(exc)).split())
    return (text[:300] + "…" if len(text) > 300 else text) or type(exc).__name__


def _display_url(url: str) -> str:
    """Адрес без параметров и фрагмента — в них бывают токены (Telegram —
    в пути: /bot<токен>/ маскируется как в api_log)."""
    from .api_log import mask_url

    parts = urlsplit(mask_url(url))
    path = parts.path if len(parts.path) <= 60 else parts.path[:57] + "…"
    return f"{parts.netloc}{path}"


# --- HTTP-запросы внутри шага --------------------------------------------------

_local = threading.local()
_hook_installed = False


def _current() -> Progress | None:
    return getattr(_local, "progress", None)


def install_http_hook() -> None:
    """Обёртка над requests.Session.send: запрос из потока, где идёт
    операция с окном, виден в окне. Ставится один раз; с app/api_log.py
    совместима в любом порядке (каждая оборачивает то, что было до неё)."""
    global _hook_installed
    if _hook_installed:
        return
    _hook_installed = True
    previous = requests.Session.send

    def send(self, req, **kwargs):
        progress = _current()
        if progress is None or not progress.active:
            return previous(self, req, **kwargs)
        sub_id = progress.http_started(req.method, req.url)
        started = time.monotonic()
        try:
            resp = previous(self, req, **kwargs)
        except requests.Timeout as exc:
            progress.http_finished(sub_id, req.method, req.url, FAIL,
                                   f"сервер не ответил вовремя ({round(time.monotonic() - started)} с)")
            raise
        except requests.ConnectionError as exc:
            progress.http_finished(sub_id, req.method, req.url, FAIL, _connection_error_text(exc))
            raise
        except Exception as exc:
            progress.http_finished(sub_id, req.method, req.url, FAIL, _exc_text(exc))
            raise
        elapsed = round((time.monotonic() - started) * 1000)
        status = OK if resp.status_code < 400 else WARN
        progress.http_finished(sub_id, req.method, req.url, status, f"{resp.status_code}, {elapsed} мс")
        return resp

    requests.Session.send = send


def _connection_error_text(exc: Exception) -> str:
    text = str(exc)
    if "SSLError" in text or "CERTIFICATE" in text:
        return "ошибка TLS (сертификат или шифрование)"
    if "NameResolution" in text or "getaddrinfo" in text or "Name or service not known" in text:
        return "адрес сервера не найден (DNS)"
    if "Connection refused" in text:
        return "сервер отказал в подключении"
    if "timed out" in text:
        return "сервер не ответил вовремя"
    if "Network is unreachable" in text:
        return "сеть недоступна: с сервера нет маршрута до этого адреса"
    return "нет соединения: " + _exc_text(exc)


@contextmanager
def observe(progress: Progress):
    """HTTP-запросы этого потока — в окно progress."""
    previous, _local.progress = _current(), progress
    try:
        yield
    finally:
        _local.progress = previous


# --- роут ----------------------------------------------------------------------

def wants_stream() -> bool:
    return NDJSON in request.headers.get("Accept", "")


def respond(task):
    """Итог роута: поток событий для окна или flash + redirect без JS."""
    if not wants_stream():
        result = task(NULL)
        flash(result.message, result.flash_category())
        return redirect(result.redirect)
    return stream(task)


def stream(task) -> Response:
    events: queue.Queue = queue.Queue()
    user_id = g.user.id if g.get("user") else None
    locale = g.get("locale")

    @copy_current_request_context
    def worker():
        # Своё app-контекст и сессия БД (scoped_session — по потоку):
        # g.user и язык — как у запроса; уведомления правлению (их шлёт
        # after_request) — здесь же, после коммита задачи.
        from . import database, notifications
        from .models import User

        g.locale = locale
        g.user = database.db_session.get(User, user_id) if user_id else None
        progress = Progress(events.put)
        try:
            with observe(progress):
                result = task(progress)
            notifications.dispatch_after_commit()
            events.put({"type": "done", "ok": result.ok, "message": result.message,
                        "redirect": result.redirect, "category": result.flash_category()})
        except Exception as exc:  # непредвиденное — в окно и в лог, не тишина
            from flask import current_app
            current_app.logger.exception("Сбой операции с окном прогресса")
            database.db_session.rollback()
            events.put({"type": "done", "ok": False, "message": f"Внутренняя ошибка: {_exc_text(exc)}",
                        "redirect": None, "category": "danger"})

    # Транзакцию запроса — закрыть до старта задачи: проверки роута могли
    # создать строку (_get_or_create_settings) или просто читать, а SQLite
    # не даст рабочему потоку писать, пока эта сессия держит блокировку
    # («database is locked»). Без окна эти изменения тоже ушли бы в commit.
    from . import database
    database.db_session.commit()

    thread = threading.Thread(target=worker, name="progress-task", daemon=True)
    thread.start()

    def body():
        while True:
            try:
                event = events.get(timeout=PING_INTERVAL)
            except queue.Empty:
                yield json.dumps({"type": "ping"}) + "\n"
                continue
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
            if event["type"] == "done":
                return

    return Response(stream_with_context(body()), mimetype=NDJSON,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def stream_events(events_iter, done) -> Response:
    """Готовый генератор событий (app/mail_diagnostics.py) — в том же
    формате; done(event) дополняет финальное событие (message, redirect)."""
    def body():
        for event in events_iter:
            if event.get("type") == "done":
                event = done(event)
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"

    return Response(stream_with_context(body()), mimetype=NDJSON,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
