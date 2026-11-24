"""Светлая/тёмная тема интерфейса — хранится в сессии, по аналогии с языком (см. i18n.py).

Режимов три: светлая, тёмная и «как в системе» (auto, по умолчанию — пока
кнопкой ничего не выбрано). В auto сервер не знает тему ОС, поэтому отдаёт
DEFAULT_THEME, а скрипт в <head> base.html ставит тему по prefers-color-scheme
до отрисовки и следит за её сменой.
"""
from flask import session, g, redirect, request

DEFAULT_THEME = "light"
AUTO_THEME = "auto"
SUPPORTED_THEMES = {"light": "Светлая", "dark": "Тёмная"}
THEME_MODES = (*SUPPORTED_THEMES, AUTO_THEME)
# Пиктограмма показывает режим, В КОТОРЫЙ переключит клик (а не текущий) —
# так кнопка выглядит как переключатель действия, а не индикатор состояния.
# Имена иконок Bootstrap Icons (bi-<имя>).
THEME_TOGGLE_ICONS = {"light": "sun", "dark": "moon-stars", AUTO_THEME: "circle-half"}
# Подсказка на кнопке — тоже про то, что сделает клик.
THEME_TOGGLE_TITLES = {
    "light": "Включить светлую тему",
    "dark": "Включить тёмную тему",
    AUTO_THEME: "Тема как в системе",
}


def get_theme_mode() -> str:
    """Выбранный режим: light/dark или auto, если кнопкой ничего не выбрано."""
    theme = session.get("theme")
    return theme if theme in SUPPORTED_THEMES else AUTO_THEME


def get_theme() -> str:
    mode = get_theme_mode()
    return DEFAULT_THEME if mode == AUTO_THEME else mode


def next_theme_mode(mode: str, os_theme: str = DEFAULT_THEME) -> str:
    """Следующий режим по клику. Цикл зависит от темы ОС, чтобы первый клик
    всегда менял вид: auto → противоположная ОС → тема ОС → auto.
    Серверу тема ОС неизвестна (берём DEFAULT_THEME) — скрипт в base.html
    пересчитывает ссылку той же логикой."""
    other = "dark" if os_theme == "light" else "light"
    if mode == AUTO_THEME:
        return other
    if mode == other:
        return os_theme
    return AUTO_THEME


def init_app(app):
    @app.before_request
    def _set_theme():
        g.theme_mode = get_theme_mode()
        g.theme = get_theme()

    app.jinja_env.globals["SUPPORTED_THEMES"] = SUPPORTED_THEMES
    app.jinja_env.globals["THEME_TOGGLE_ICONS"] = THEME_TOGGLE_ICONS
    app.jinja_env.globals["THEME_TOGGLE_TITLES"] = THEME_TOGGLE_TITLES
    app.jinja_env.globals["THEME_MODES"] = THEME_MODES
    app.jinja_env.globals["next_theme_mode"] = next_theme_mode

    @app.context_processor
    def _inject_theme():
        return {
            "current_theme": getattr(g, "theme", DEFAULT_THEME),
            "theme_mode": getattr(g, "theme_mode", AUTO_THEME),
        }

    @app.route("/set-theme/<theme>")
    def set_theme(theme):
        if theme in SUPPORTED_THEMES:
            session["theme"] = theme
        elif theme == AUTO_THEME:
            session.pop("theme", None)
        return redirect(request.referrer or "/")
