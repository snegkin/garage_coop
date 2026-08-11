"""Светлая/тёмная тема интерфейса — хранится в сессии, по аналогии с языком (см. i18n.py)."""
from flask import session, g, redirect, request

DEFAULT_THEME = "light"
SUPPORTED_THEMES = {"light": "Светлая", "dark": "Тёмная"}


def get_theme() -> str:
    theme = session.get("theme")
    return theme if theme in SUPPORTED_THEMES else DEFAULT_THEME


def init_app(app):
    @app.before_request
    def _set_theme():
        g.theme = get_theme()

    app.jinja_env.globals["SUPPORTED_THEMES"] = SUPPORTED_THEMES

    @app.context_processor
    def _inject_theme():
        return {"current_theme": getattr(g, "theme", DEFAULT_THEME)}

    @app.route("/set-theme/<theme>")
    def set_theme(theme):
        if theme in SUPPORTED_THEMES:
            session["theme"] = theme
        return redirect(request.referrer or "/")
