"""
Простая аутентификация на сессиях Flask (без внешних зависимостей вроде
Flask-Login) + декораторы для ограничения доступа по ролям.
"""
import re
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import check_password_hash, generate_password_hash

from . import database
from . import audit
from .rate_limit import limiter
from .i18n import translate as _
from .models import User, RoleEnum, Person, Phone
from .login_generation import generate_unique_login

bp = Blueprint("auth", __name__, url_prefix="/auth")

_PHONE_NON_DIGIT_RE = re.compile(r"\D")


def _normalize_phone_digits(raw: str) -> str:
    """Телефон -> только цифры, российский формат приведён к 10 цифрам без
    кода страны/восьмёрки (+7.../8... -> 10 последних цифр) — номера в
    Phone.number вводятся людьми в произвольном формате (см.
    contact_format.py: поля без валидации), при входе по телефону сравнение
    должно проходить независимо от того, как именно номер записан."""
    digits = _PHONE_NON_DIGIT_RE.sub("", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return digits


def _person_by_phone_digits(digits: str) -> "Person | None":
    """Person, у которого есть Phone с таким же нормализованным номером —
    None и если не нашли, и если нашлось НЕСКОЛЬКО РАЗНЫХ людей (в т.ч. из-за
    опечатки при вводе телефона одному из них) — при неоднозначности
    безопаснее отказать, чем угадывать, в чей аккаунт входить/который
    создавать."""
    candidates = (
        database.db_session.query(Phone)
        .join(Person, Phone.person_id == Person.id)
        .all()
    )
    matched_person_ids = {p.person_id for p in candidates if _normalize_phone_digits(p.number) == digits}
    if len(matched_person_ids) != 1:
        return None
    return database.db_session.get(Person, matched_person_ids.pop())


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


def _complete_login(user: User, summary: str):
    """Общий хвост успешного входа — заводит сессию, пишет аудит,
    редиректит на next (если безопасный) или на дашборд. Используется и
    обычным входом по логину, и входом по телефону."""
    session.clear()
    session["user_id"] = user.id
    audit.record("auth.login", entity_type="user", entity_id=user.id, summary=summary, actor=user)
    database.db_session.commit()
    next_url = request.args.get("next")
    if is_safe_next_url(next_url):
        return redirect(next_url)
    return redirect(url_for("main.dashboard"))


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
            return _complete_login(user, f"Успешный вход: «{username}»")

    # Страница входа — де-факто главная страница сайта (анонимный посетитель
    # всегда попадает сюда, см. index()/dashboard() в main.py), поэтому
    # новостная лента правления показывается прямо здесь.
    from .news import latest_news
    return render_template("auth/login.html", news_items=latest_news())


@bp.route("/login-phone", methods=["POST"])
@limiter.limit("10 per minute")
def login_by_phone():
    """
    Вход по номеру телефона — альтернатива логину/паролю на странице входа
    (см. auth/login.html, вкладка «По телефону»). Номер сверяется со всеми
    Phone кооператива (см. _person_by_phone_digits) — не полем User, у
    которого своего номера нет вовсе, только связь с Person.

    Если у найденного Person ещё нет учётной записи — она создаётся здесь
    же, "самостоятельной регистрацией": логин — по тем же правилам, что и
    в мастере массового создания (setup_wizard.py), но коллизии, которые
    там показываются человеку для ручной правки, здесь разрешаются
    автоматической эскалацией (см. login_generation.generate_unique_login)
    — попросить кого-то разрешить коллизию тут некому. Пароль — тот,
    что человек только что ввёл в форму САМ (не генерируется случайно, в
    отличие от мастера, — здесь его никто, кроме самого человека, не увидит
    и вводить второй раз для входа не придётся).

    Если учётная запись уже есть — обычная проверка пароля, как и при
    входе по логину, без каких-либо шагов регистрации.

    Осознанный компромисс: единственное подтверждение личности —
    сам факт знания номера телефона, уже занесённого в карточку человека
    (без SMS-кода — в проекте нет SMS-интеграции). Это ниже, чем при входе
    по личному логину/паролю, но соответствует тому, что телефон и так не
    публичные данные (Phone виден только правлению — см. persons.py), и
    даёт члену кооператива завести себе доступ самому, не дожидаясь, пока
    председатель проведёт его через мастер настройки.
    """
    phone = request.form.get("phone", "").strip()
    password = request.form.get("password", "")
    digits = _normalize_phone_digits(phone)

    if len(digits) < 7 or not password:
        flash(_("Введите номер телефона и пароль."), "danger")
        return redirect(url_for("auth.login"))

    person = _person_by_phone_digits(digits)
    if person is None:
        audit.record("auth.login_failed", summary=f"Неудачная попытка входа по телефону: «{phone}»")
        database.db_session.commit()
        flash(_("Не удалось войти по этому номеру телефона."), "danger")
        return redirect(url_for("auth.login"))

    user = database.db_session.query(User).filter_by(person_id=person.id).first()

    if user is None:
        existing_usernames = {u for (u,) in database.db_session.query(User.username)}
        username = generate_unique_login(person.full_name, existing_usernames)
        initial_role = RoleEnum.CHAIRMAN if person.is_chairman else (
            RoleEnum.ACCOUNTANT if person.is_accountant else (
                RoleEnum.BOARD if person.is_board_member else RoleEnum.MEMBER
            )
        )
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            role=initial_role,
            person_id=person.id,
            is_active=True,
        )
        database.db_session.add(user)
        database.db_session.flush()
        audit.record(
            "account.self_register_by_phone", entity_type="user", entity_id=user.id,
            summary=f"Учётная запись «{username}» создана самостоятельно по номеру телефона для {person.full_name}",
        )
        return _complete_login(user, f"Успешный вход по телефону (новая учётная запись «{username}»)")

    if not check_password_hash(user.password_hash, password):
        audit.record(
            "auth.login_failed", entity_type="user", entity_id=user.id,
            summary=f"Неудачная попытка входа по телефону: «{phone}»", actor=user,
        )
        database.db_session.commit()
        flash(_("Неверный пароль."), "danger")
        return redirect(url_for("auth.login"))

    if not user.is_active:
        audit.record(
            "auth.login_failed", entity_type="user", entity_id=user.id,
            summary=f"Попытка входа по телефону в отключённую учётную запись «{user.username}»", actor=user,
        )
        database.db_session.commit()
        flash(_("Учётная запись отключена."), "danger")
        return redirect(url_for("auth.login"))

    return _complete_login(user, f"Успешный вход по телефону: «{user.username}»")


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
