"""
Обработчики ошибок уровня приложения.

Две разные задачи в одном модуле:

1. Человеческие страницы для 403/404/500/CSRF — вместо голого traceback
   Flask/Werkzeug или стандартной страницы-заглушки.

2. Защитная сетка от кривого ввода в формах. По всему проекту порядка 60+
   мест вида `Decimal(f["amount"])` / `int(f["year"])` /
   `dt.date.fromisoformat(f["date"])` без try/except — любое нечисловое
   значение, пустая строка или отсутствующее поле формы роняет запрос в
   500 с голым traceback (или, что хуже, с деталями реализации, если кто-то
   всё же включит debug=True на проде). Переписывать все ~60 мест по
   отдельности — большой объём правок с riском неровно расставленных
   try/except; вместо этого ловим сам класс типичных исключений на уровне
   приложения и превращаем их в понятное сообщение + возврат на ту же
   форму, а не в 500. Это НЕ подменяет валидацию (например, "начислили
   отрицательную сумму" всё ещё пройдёт) — только не даёт кривому вводу
   ронять процесс.
"""
import logging

from decimal import InvalidOperation

from flask import render_template, request, redirect, flash, g
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

from .i18n import translate as _

logger = logging.getLogger("garage_coop")


def _redirect_back():
    """Возвращает пользователя туда, откуда он отправил форму, если это
    безопасный внутренний путь — иначе на дашборд."""
    from .auth import is_safe_next_url
    referrer = request.referrer
    if referrer:
        from urllib.parse import urlparse
        path = urlparse(referrer).path
        if is_safe_next_url(path):
            return redirect(path)
    from flask import url_for
    return redirect(url_for("main.dashboard"))


def register_error_handlers(app):
    @app.errorhandler(403)
    def _forbidden(e):
        return render_template(
            "error.html", code=403, title=_("Доступ запрещён"),
            message=_("У вас нет прав для просмотра этой страницы."),
        ), 403

    @app.errorhandler(404)
    def _not_found(e):
        return render_template(
            "error.html", code=404, title=_("Страница не найдена"),
            message=_("Такой страницы не существует, либо она была удалена."),
        ), 404

    @app.errorhandler(500)
    def _server_error(e):
        logger.exception("Unhandled server error")
        from . import database
        database.db_session.rollback()
        return render_template(
            "error.html", code=500, title=_("Непредвиденная ошибка"),
            message=_("Что-то пошло не так на сервере. Попробуйте ещё раз или обратитесь в правление."),
        ), 500

    @app.errorhandler(CSRFError)
    def _csrf_error(e):
        """302, не 400: пользователь работает в обычном браузере (не через
        JS/fetch), и нам нужно, чтобы браузер реально проследовал по
        редиректу, а не показал страницу-заглушку с ошибкой — 400 с телом
        редиректа браузер автоматически не подхватывает."""
        flash(_("Сессия формы устарела или недействительна — обновите страницу и попробуйте снова."), "warning")
        return _redirect_back()

    @app.errorhandler(ValueError)
    @app.errorhandler(InvalidOperation)
    @app.errorhandler(KeyError)
    def _bad_form_input(e):
        """Ловит нечисловые суммы/годы, кривые даты, отсутствующие обязательные
        поля — см. docstring модуля. Логируется с деталями для отладки, но
        пользователь видит только понятное сообщение, не голый traceback.
        Обычный редирект (302), не 400 — по той же причине, что и в
        _csrf_error выше."""
        logger.warning("Bad form input on %s %s: %r", request.method, request.path, e)
        from . import database
        database.db_session.rollback()
        flash(
            _("Проверьте правильность заполнения формы — одно из полей заполнено некорректно или не заполнено."),
            "danger",
        )
        return _redirect_back()
