"""
Простая аутентификация на сессиях Flask (без внешних зависимостей вроде
Flask-Login) + декораторы для ограничения доступа по ролям.
"""
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import check_password_hash, generate_password_hash

from . import database
from . import audit
from .rate_limit import limiter
from .i18n import translate as _
from .models import User, RoleEnum

bp = Blueprint("auth", __name__, url_prefix="/auth")


def is_safe_next_url(next_url: str | None) -> bool:
    """
    Проверяет, что `next` — это относительный путь внутри нашего сайта, а не
    ссылка на внешний домен. Одного `startswith("/")` недостаточно: браузер
    трактует "//evil.com" и "/\\evil.com" как переход на другой хост
    (protocol-relative URL), поэтому такие варианты отдельно отсекаем.
    Используется во всех местах, где после действия делаем redirect(next).
    """
    if not next_url:
        return False
    if not next_url.startswith("/"):
        return False
    if next_url.startswith("//") or next_url.startswith("/\\"):
        return False
    return True

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
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = database.db_session.query(User).filter_by(username=username).first()

        if user is None or not check_password_hash(user.password_hash, password):
            audit.record(
                "auth.login_failed", summary=f"Неудачная попытка входа: логин «{username}»",
            )
            database.db_session.commit()
            flash(_("Неверный логин или пароль."), "danger")
        elif not user.is_active:
            audit.record(
                "auth.login_failed", entity_type="user", entity_id=user.id,
                summary=f"Попытка входа в отключённую учётную запись «{username}»", actor=user,
            )
            database.db_session.commit()
            flash(_("Учётная запись отключена."), "danger")
        else:
            session.clear()
            session["user_id"] = user.id
            audit.record(
                "auth.login", entity_type="user", entity_id=user.id,
                summary=f"Успешный вход: «{username}»", actor=user,
            )
            database.db_session.commit()
            next_url = request.args.get("next")
            if is_safe_next_url(next_url):
                return redirect(next_url)
            return redirect(url_for("main.dashboard"))

    # Страница входа — де-факто главная страница сайта (анонимный посетитель
    # всегда попадает сюда, см. index()/dashboard() в main.py), поэтому
    # новостная лента правления показывается прямо здесь.
    from .news import latest_news
    return render_template("auth/login.html", news_items=latest_news())


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def force_change_password():
    """Принудительная смена пароля — единственная страница, доступная
    пользователю с must_change_password=True (см. app/__init__.py:
    _enforce_password_change, редиректит сюда с любой другой страницы).
    В отличие от cabinet.change_password не спрашивает текущий пароль —
    личность уже подтверждена самим входом в систему с ним."""
    if not g.user.must_change_password:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(new_password) < 4:
            flash(_("Новый пароль слишком короткий (минимум 4 символа)."), "danger")
            return redirect(url_for("auth.force_change_password"))
        if new_password != confirm_password:
            flash(_("Новый пароль и подтверждение не совпадают."), "danger")
            return redirect(url_for("auth.force_change_password"))

        g.user.password_hash = generate_password_hash(new_password)
        g.user.must_change_password = False
        audit.record(
            "account.password_change", entity_type="user", entity_id=g.user.id,
            summary=f"Пользователь «{g.user.username}» сменил пароль при первом входе",
        )
        database.db_session.commit()
        flash(_("Пароль изменён."), "success")
        return redirect(url_for("main.dashboard"))

    return render_template("auth/force_change_password.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
