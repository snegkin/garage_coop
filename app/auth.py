"""
Простая аутентификация на сессиях Flask (без внешних зависимостей вроде
Flask-Login) + декораторы для ограничения доступа по ролям.
"""
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import check_password_hash

from . import database
from .i18n import translate as _
from .models import User, RoleEnum

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Иерархия ролей: председатель видит всё, что и правление; правление — всё, что и член.
ROLE_LEVEL = {RoleEnum.MEMBER: 0, RoleEnum.BOARD: 1, RoleEnum.ACCOUNTANT: 1, RoleEnum.CHAIRMAN: 2}


def load_logged_in_user():
    """Вызывается перед каждым запросом (см. app/__init__.py) — кладёт текущего пользователя в g.user."""
    user_id = session.get("user_id")
    g.user = database.db_session.get(User, user_id) if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash(_("Пожалуйста, войдите в систему."), "warning")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles: RoleEnum):
    """Разрешает доступ, если роль пользователя не ниже минимальной из переданных ролей."""
    min_level = min(ROLE_LEVEL[r] for r in roles)

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if ROLE_LEVEL[g.user.role] < min_level:
                flash(_("Недостаточно прав для этого действия."), "danger")
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = database.db_session.query(User).filter_by(username=username).first()

        if user is None or not check_password_hash(user.password_hash, password):
            flash(_("Неверный логин или пароль."), "danger")
        elif not user.is_active:
            flash(_("Учётная запись отключена."), "danger")
        else:
            session.clear()
            session["user_id"] = user.id
            return redirect(request.args.get("next") or url_for("main.dashboard"))

    return render_template("auth/login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
