"""Светлая/тёмная тема интерфейса — хранится в сессии, по аналогии с языком (см. i18n.py).

Пока пользователь не выбрал тему кнопкой, она берётся из настроек ОС/браузера
(prefers-color-scheme): сервер этого не знает, поэтому отдаёт DEFAULT_THEME,
а скрипт в <head> base.html подменяет её до отрисовки и следит за сменой.
"""
from flask import session, g, redirect, request

DEFAULT_THEME = "light"
SUPPORTED_THEMES = {"light": "Светлая", "dark": "Тёмная"}
# Пиктограмма показывает тему, В КОТОРУЮ переключит клик (а не текущую) —
# так кнопка выглядит как переключатель действия, а не индикатор состояния.
# Имена иконок Bootstrap Icons (bi-<имя>).
THEME_TOGGLE_ICONS = {"light": "moon-stars", "dark": "sun"}


def get_forced_theme() -> str | None:
    """Тема, выбранная кнопкой; None — следовать настройкам ОС."""
    theme = session.get("theme")
    return theme if theme in SUPPORTED_THEMES else None


def get_theme() -> str:
    return get_forced_theme() or DEFAULT_THEME


def next_theme(current: str) -> str:
    """Единственная другая тема — при двух темах переключатель просто меняет местами."""
    others = [t for t in SUPPORTED_THEMES if t != current]
    return others[0] if others else current


def init_app(app):
    @app.before_request
    def _set_theme():
        g.theme = get_theme()
        g.theme_auto = get_forced_theme() is None

    app.jinja_env.globals["SUPPORTED_THEMES"] = SUPPORTED_THEMES
    app.jinja_env.globals["THEME_TOGGLE_ICONS"] = THEME_TOGGLE_ICONS
    app.jinja_env.globals["next_theme"] = next_theme

    @app.context_processor
    def _inject_theme():
        return {
            "current_theme": getattr(g, "theme", DEFAULT_THEME),
            "theme_auto": getattr(g, "theme_auto", True),
        }

    @app.route("/set-theme/<theme>")
    def set_theme(theme):
        if theme in SUPPORTED_THEMES:
            session["theme"] = theme
        return redirect(request.referrer or "/")
