"""
Простая аутентификация на сессиях Flask (без внешних зависимостей вроде
Flask-Login) + декораторы для ограничения доступа по ролям.
"""
import re
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import func

from . import database
from . import audit
from . import mail_client
from . import verification
from . import recaptcha
from .mail_client import MailError
from .rate_limit import limiter
from .i18n import translate as _
from .models import User, RoleEnum, Person, Phone, MailboxSettings, SmsSettings, Cooperative, VerificationCode, VerificationCodePurpose
from .login_generation import generate_unique_login
from .sms import get_sms_client, SmsError, sms_site_identifier

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


def _sms_code_text(label: str, code: str) -> str:
    """Текст SMS с кодом — с названием/сайтом кооператива в скобках (см.
    sms.sms_site_identifier): при отправке от чужого/бесплатного имени
    отправителя операторы блокируют сообщения вида "Код: 1234" без
    названия компании или адреса сайта (требование SMS Aero). label —
    уже переведённая строка ("Код подтверждения"/"Код для восстановления
    пароля"), само склеивание — не лингвистический контент, перевод не
    нужен."""
    coop = database.db_session.query(Cooperative).first()
    site = sms_site_identifier(coop)
    return f"{label}: {code} ({site})" if site else f"{label}: {code}"


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


def _person_by_email(email_lower: str) -> "Person | None":
    """Тот же принцип неоднозначности, что и у _person_by_phone_digits —
    Person.email не unique на уровне БД (см. models.py), поэтому если
    несколько разных карточек ошибочно указали один email, отказываем, а
    не угадываем."""
    matches = (
        database.db_session.query(Person)
        .filter(func.lower(Person.email) == email_lower)
        .all()
    )
    if len(matches) != 1:
        return None
    return matches[0]


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
    редиректит на next (если безопасный), иначе — на главную (по прямой
    просьбе не уводить рядового члена на дашборд/«мои гаражи»
    принудительно, см. main.index), а для правления/председателя — по
    прежнему на дашборд (это их рабочая главная, см. main.dashboard).
    g.user на этот момент ещё не обновлён (before_request отработал ДО
    входа в этот же запрос) — роль берём из уже известного user, не через
    permissions.is_board()/g.user. Используется и обычным входом по
    логину, и входом по телефону."""
    session.clear()
    session["user_id"] = user.id
    audit.record("auth.login", entity_type="user", entity_id=user.id, summary=summary, actor=user)
    database.db_session.commit()
    next_url = request.args.get("next")
    if is_safe_next_url(next_url):
        return redirect(next_url)
    if ROLE_LEVEL[user.role] >= ROLE_LEVEL[RoleEnum.BOARD]:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("main.index"))


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = database.db_session.query(User).filter_by(username=username).first()

        if user is None or not check_password_hash(user.password_hash, password):
            summary = f"Неудачная попытка входа: логин «{username}»"
            if user is None:
                # Похоже, человек перепутал вкладки и ввёл номер телефона в
                # поле обычного логина (а не воспользовался вкладкой «По
                # телефону») — если такой номер привязан к чьей-то карточке
                # с учётной записью, подсказываем в журнале её логин, чтобы
                # председатель сразу видел, кому помочь, а не гадал по
                # голому номеру.
                phone_digits = _normalize_phone_digits(username)
                if len(phone_digits) >= 7:
                    phone_person = _person_by_phone_digits(phone_digits)
                    phone_user = (
                        database.db_session.query(User).filter_by(person_id=phone_person.id).first()
                        if phone_person is not None else None
                    )
                    if phone_user is not None:
                        summary += f" (телефон привязан к «{phone_user.username}»)"
            audit.record("auth.login_failed", summary=summary)
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

    # Сама форма входа теперь ещё и всегда доступна дропдауном в шапке
    # (см. base.html, auth/_login_form.html) — эта страница нужна как
    # реальная цель редиректа login_required (next=...) и для прямых
    # переходов (например, «Забыли пароль?»). Новости и видеонаблюдение
    # переехали на главную (main.index), здесь их больше нет.
    return render_template("auth/login.html")


@bp.route("/login-phone", methods=["POST"])
@limiter.limit("10 per minute")
def login_by_phone():
    """
    Вход по номеру телефона — альтернатива логину/паролю на странице входа
    (см. auth/login.html, вкладка «По телефону»). Номер сверяется со всеми
    Phone кооператива (см. _person_by_phone_digits) — не полем User, у
    которого своего номера нет вовсе, только связь с Person.

    Если у найденного Person ещё нет учётной записи — самостоятельная
    регистрация, но не сразу: сначала подтверждение номера СМС-кодом (см.
    register_phone_confirm ниже) — иначе создать себе доступ мог бы кто
    угодно, кто просто знает чужой номер телефона, занесённый в карточку
    человека правлением. Логин при этом — по тем же правилам, что и в
    мастере массового создания (setup_wizard.py), коллизии разрешаются
    автоматической эскалацией (см. login_generation.generate_unique_login)
    — попросить кого-то разрешить коллизию тут некому, в отличие от
    мастера. Пароль — тот, что человек ввёл здесь сам (хэшируется сразу и
    хранится в payload кода подтверждения — см. app/verification.py,
    ни разу не гоняется через браузер повторно между этим шагом и
    подтверждением кода).

    Если учётная запись уже есть — обычная проверка пароля, как и при
    входе по логину, без кода — телефон здесь уже не средство
    регистрации, а просто альтернативный идентификатор для уже
    подтверждённого раньше аккаунта.
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
        sms_settings = database.db_session.query(SmsSettings).first()
        client = get_sms_client(sms_settings)
        if client is None:
            flash(_("СМС-уведомления пока не настроены — обратитесь к председателю."), "danger")
            return redirect(url_for("auth.login"))

        code = verification.issue_code(
            VerificationCodePurpose.PHONE_REGISTER, digits, payload=generate_password_hash(password),
        )
        try:
            client.send(digits, _sms_code_text(_("Код подтверждения"), code))
        except SmsError as exc:
            database.db_session.rollback()
            flash(_("Не удалось отправить СМС: {error}", error=str(exc)), "danger")
            return redirect(url_for("auth.login"))
        database.db_session.commit()
        return render_template("auth/verify_phone_code.html", phone=phone)

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


@bp.route("/register-phone/resend", methods=["POST"])
@limiter.limit("5 per minute")
def register_phone_resend():
    """«Отправить код ещё раз» на странице ввода кода (auth/verify_phone_code.html)
    — только телефон, БЕЗ пароля: пароль уже осел хэшем в payload
    непогашенного кода с первого запроса (см. login_by_phone), берём его
    оттуда, а не просим ввести снова."""
    phone = request.form.get("phone", "").strip()
    digits = _normalize_phone_digits(phone)
    if len(digits) < 7:
        flash(_("Некорректный номер телефона."), "danger")
        return redirect(url_for("auth.login"))

    # Столбец, не вся сущность — existing_payload дальше передаётся в
    # issue_code(), который эту же строку удалит (см. docstring
    # verification.issue_code); если бы здесь была загружена целая ORM-
    # сущность, SQLAlchemy предупредил бы о повторном использовании id
    # только что удалённой строки для новой (SQLite переиспользует rowid
    # опустевшей таблицы) — так этой сущности просто не существует.
    existing_payload = (
        database.db_session.query(VerificationCode.payload)
        .filter(
            VerificationCode.purpose == VerificationCodePurpose.PHONE_REGISTER,
            VerificationCode.target == digits,
            VerificationCode.consumed_at.is_(None),
        )
        .order_by(VerificationCode.id.desc())
        .limit(1)
        .scalar()
    )
    if existing_payload is None:
        flash(_("Запросите код заново, указав телефон и пароль."), "danger")
        return redirect(url_for("auth.login"))

    sms_settings = database.db_session.query(SmsSettings).first()
    client = get_sms_client(sms_settings)
    if client is None:
        flash(_("СМС-уведомления пока не настроены — обратитесь к председателю."), "danger")
        return redirect(url_for("auth.login"))

    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, digits, payload=existing_payload)
    try:
        client.send(digits, _sms_code_text(_("Код подтверждения"), code))
    except SmsError as exc:
        database.db_session.rollback()
        flash(_("Не удалось отправить СМС: {error}", error=str(exc)), "danger")
        return redirect(url_for("auth.login"))
    database.db_session.commit()
    flash(_("Код отправлен повторно."), "success")
    return render_template("auth/verify_phone_code.html", phone=phone)


@bp.route("/register-phone/confirm", methods=["POST"])
@limiter.limit("10 per minute")
def register_phone_confirm():
    """Подтверждение кода из СМС — довершает самостоятельную регистрацию,
    начатую в login_by_phone. Пароль сюда уже не передаётся — он хэширован
    и лежит в payload кода (см. verification.consume_code)."""
    phone = request.form.get("phone", "").strip()
    code = request.form.get("code", "").strip()
    digits = _normalize_phone_digits(phone)

    person = _person_by_phone_digits(digits)
    ok, password_hash = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, digits, code)
    if not ok or person is None or password_hash is None:
        database.db_session.commit()  # попытка (attempts) должна сохраниться, даже если код неверный
        flash(_("Неверный или истёкший код."), "danger")
        return render_template("auth/verify_phone_code.html", phone=phone)

    # На случай, если аккаунт уже успели создать другим путём между
    # запросом кода и его подтверждением (например, открыли форму в двух
    # вкладках) — не создаём второй.
    existing_user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if existing_user is not None:
        database.db_session.commit()
        flash(_("Учётная запись для этого номера уже существует — войдите по логину или телефону."), "danger")
        return redirect(url_for("auth.login"))

    existing_usernames = {u for (u,) in database.db_session.query(User.username)}
    username = generate_unique_login(person.full_name, existing_usernames)
    initial_role = RoleEnum.CHAIRMAN if person.is_chairman else (
        RoleEnum.ACCOUNTANT if person.is_accountant else (
            RoleEnum.BOARD if person.is_board_member else RoleEnum.MEMBER
        )
    )
    user = User(
        username=username, password_hash=password_hash, role=initial_role,
        person_id=person.id, is_active=True,
    )
    database.db_session.add(user)
    database.db_session.flush()
    audit.record(
        "account.self_register_by_phone", entity_type="user", entity_id=user.id,
        summary=f"Учётная запись «{username}» создана самостоятельно по номеру телефона для {person.full_name} (подтверждено СМС-кодом)",
    )
    return _complete_login(user, f"Успешный вход по телефону (новая учётная запись «{username}»)")


# ---------------------------------------------------------------------------
# Восстановление пароля — по email или по телефону (СМС)
# ---------------------------------------------------------------------------

def _looks_like_email(identifier: str) -> bool:
    return "@" in identifier


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def forgot_password():
    """
    Запрос на восстановление пароля — по email ИЛИ телефону (канал
    определяется по виду введённого идентификатора: есть "@" — email,
    иначе — телефон), защищено reCAPTCHA v2 (см. app/recaptcha.py).

    Показывает ОДНО И ТО ЖЕ сообщение независимо от того, нашёлся ли
    аккаунт — не подтверждаем и не опровергаем существование учётной
    записи по введённому email/телефону (защита от перебора чужих
    данных на этой форме).
    """
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        token = request.form.get("g-recaptcha-response", "")

        if not identifier:
            flash(_("Введите email или номер телефона."), "danger")
            return render_template("auth/forgot_password.html")

        if not recaptcha.verify(token, request.remote_addr):
            flash(_("Не пройдена проверка «Я не робот» — попробуйте ещё раз."), "danger")
            return render_template("auth/forgot_password.html")

        # Дальше — ОДИН выход независимо от того, нашёлся ли аккаунт и
        # удалось ли реально отправить код (не настроена почта/СМС,
        # сетевая ошибка): и статус ответа, и сообщение, и редирект должны
        # быть одинаковыми во всех случаях — иначе сам факт, что ответ
        # иначе оформлен (200 вместо 302 и т.п.), уже выдаёт, существует
        # ли аккаунт с таким email/телефоном, сводя на нет весь смысл
        # общего сообщения ниже.
        if _looks_like_email(identifier):
            target = identifier.lower()
            person = _person_by_email(target)
        else:
            target = _normalize_phone_digits(identifier)
            person = _person_by_phone_digits(target) if len(target) >= 7 else None

        user = database.db_session.query(User).filter_by(person_id=person.id).first() if person else None

        if user is not None:
            code = verification.issue_code(VerificationCodePurpose.PASSWORD_RESET, target)
            if _looks_like_email(identifier):
                settings = database.db_session.query(MailboxSettings).first()
                if settings is not None and settings.incoming_host and settings.username and settings.password_encrypted:
                    try:
                        mail_client.send_message(
                            settings, to_addrs=[identifier], subject=_("Восстановление пароля"),
                            body_text=_("Код для восстановления пароля: {code}\n\nЕсли вы не запрашивали восстановление пароля, просто проигнорируйте это письмо.", code=code),
                        )
                    except MailError:
                        pass
            else:
                sms_settings = database.db_session.query(SmsSettings).first()
                client = get_sms_client(sms_settings)
                if client is not None:
                    try:
                        client.send(target, _sms_code_text(_("Код для восстановления пароля"), code))
                    except SmsError:
                        pass

        database.db_session.commit()
        flash(_(
            "Если такой email или номер телефона найден в базе кооператива — код для сброса пароля отправлен.",
        ), "info")
        return redirect(url_for("auth.reset_password", target=request.form.get("identifier", "").strip()))

    return render_template("auth/forgot_password.html")


@bp.route("/reset-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def reset_password():
    """Вторая половина восстановления пароля — код (из письма/СМС) + новый
    пароль. target — тот же идентификатор, что вводили на forgot_password
    (просто чтобы не заставлять вводить его снова — сам код всё равно
    привязан к конкретному normalized target, см. verification.consume_code)."""
    target_raw = request.args.get("target", "") if request.method == "GET" else request.form.get("target", "")
    target_raw = target_raw.strip()
    target = target_raw.lower() if _looks_like_email(target_raw) else _normalize_phone_digits(target_raw)

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 4:
            flash(_("Новый пароль слишком короткий (минимум 4 символа)."), "danger")
            return render_template("auth/reset_password.html", target=target_raw)
        if new_password != confirm_password:
            flash(_("Новый пароль и подтверждение не совпадают."), "danger")
            return render_template("auth/reset_password.html", target=target_raw)

        ok, _payload = verification.consume_code(VerificationCodePurpose.PASSWORD_RESET, target, code)
        if not ok:
            database.db_session.commit()
            flash(_("Неверный или истёкший код."), "danger")
            return render_template("auth/reset_password.html", target=target_raw)

        if _looks_like_email(target_raw):
            person = _person_by_email(target)
        else:
            person = _person_by_phone_digits(target)
        user = database.db_session.query(User).filter_by(person_id=person.id).first() if person else None
        if user is None:
            database.db_session.commit()
            flash(_("Не удалось найти учётную запись — обратитесь к председателю."), "danger")
            return redirect(url_for("auth.login"))

        user.password_hash = generate_password_hash(new_password)
        audit.record(
            "account.password_reset", entity_type="user", entity_id=user.id,
            summary=f"Пароль восстановлен через {'email' if _looks_like_email(target_raw) else 'СМС'}: «{user.username}»",
        )
        database.db_session.commit()
        flash(_("Пароль изменён — теперь можно войти."), "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", target=target_raw)


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
