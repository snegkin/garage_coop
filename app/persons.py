import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g
from werkzeug.security import generate_password_hash

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import Person, Phone, User, RoleEnum, MemberAccount
from .accounting import balance

bp = Blueprint("persons", __name__, url_prefix="/persons")


@bp.route("/")
@login_required
def list_persons():
    q = request.args.get("q", "").strip()
    query = database.db_session.query(Person)
    if q:
        query = query.filter(Person.full_name.ilike(f"%{q}%"))
    persons = query.order_by(Person.full_name).all()

    accounts_by_person = {}
    for account in database.db_session.query(MemberAccount).all():
        accounts_by_person.setdefault(account.person_id, []).append(account)
    balances = {
        person.id: (
            sum((balance(a) for a in accounts_by_person[person.id]), Decimal("0"))
            if person.id in accounts_by_person else None
        )
        for person in persons
    }
    return render_template("persons/list.html", persons=persons, q=q, balances=balances)


def _save_from_form(person, f):
    person.full_name = f["full_name"]
    person.registration_address = f.get("registration_address") or None
    person.residence_address = f.get("residence_address") or None
    person.email = f.get("email") or None
    person.telegram = f.get("telegram") or None
    person.passport_series = f.get("passport_series") or None
    person.passport_number = f.get("passport_number") or None
    person.passport_issued_by = f.get("passport_issued_by") or None
    issue_date = f.get("passport_issue_date")
    person.passport_issue_date = dt.date.fromisoformat(issue_date) if issue_date else None
    person.passport_department_code = f.get("passport_department_code") or None
    membership_start = f.get("membership_start_date")
    person.membership_start_date = dt.date.fromisoformat(membership_start) if membership_start else None
    membership_end = f.get("membership_end_date")
    person.membership_end_date = dt.date.fromisoformat(membership_end) if membership_end else None
    person.comment = f.get("comment") or None


def _sync_user_role(person):
    """
    Синхронизирует роль привязанной учётной записи (User.role) с флагами
    is_chairman/is_accountant/is_board_member. Без этого чекбоксы на карточке
    человека были бы чисто информационными и не влияли бы на реальные права
    входа. Приоритет при нескольких флагах: председатель > бухгалтер > правление.
    """
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        return
    if person.is_chairman:
        user.role = RoleEnum.CHAIRMAN
    elif person.is_accountant:
        user.role = RoleEnum.ACCOUNTANT
    elif person.is_board_member:
        user.role = RoleEnum.BOARD
    else:
        user.role = RoleEnum.MEMBER


def _apply_governance_flags(person, f):
    """
    Флаги "член правления", "председатель" и "бухгалтер" может менять только
    председатель (проверяем на сервере, а не только прячем чекбоксы в UI —
    иначе член правления мог бы назначить себя председателем прямым
    POST-запросом). Флаг председателя может стоять максимум у одного
    человека: если назначаем нового — снимаем флаг со всех остальных (и
    понижаем их учётные записи, если они привязаны). Председатель
    автоматически считается членом правления.
    """
    if g.user.role != RoleEnum.CHAIRMAN:
        return
    new_chairman = bool(f.get("is_chairman"))
    if new_chairman and not person.is_chairman:
        previous_chairmen = database.db_session.query(Person).filter(
            Person.id != person.id, Person.is_chairman.is_(True)
        ).all()
        for prev in previous_chairmen:
            prev.is_chairman = False
            _sync_user_role(prev)
    person.is_chairman = new_chairman
    person.is_board_member = bool(f.get("is_board_member")) or new_chairman
    person.is_accountant = bool(f.get("is_accountant"))
    _sync_user_role(person)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        person = Person(full_name="")
        _save_from_form(person, request.form)
        database.db_session.add(person)
        database.db_session.flush()
        _apply_governance_flags(person, request.form)

        phones = [p.strip() for p in request.form.get("phones", "").split(",") if p.strip()]
        for number in phones:
            database.db_session.add(Phone(person_id=person.id, number=number))

        database.db_session.commit()
        flash(_("Человек «{name}» добавлен.", name=person.full_name), "success")

        next_url = request.form.get("next")
        if next_url and next_url.startswith("/"):
            sep = "&" if "?" in next_url else "?"
            return redirect(f"{next_url}{sep}new_person_id={person.id}")
        return redirect(url_for("persons.detail", person_id=person.id))

    return render_template("persons/form.html", person=None)


@bp.route("/<int:person_id>")
@login_required
def detail(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        flash(_("Человек не найден."), "danger")
        return redirect(url_for("persons.list_persons"))
    account = database.db_session.query(User).filter_by(person_id=person.id).first()
    member_accounts = database.db_session.query(MemberAccount).filter_by(person_id=person.id).all()
    return render_template("persons/detail.html", person=person, account=account, member_accounts=member_accounts)


@bp.route("/<int:person_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        flash(_("Человек не найден."), "danger")
        return redirect(url_for("persons.list_persons"))

    if request.method == "POST":
        _save_from_form(person, request.form)
        _apply_governance_flags(person, request.form)

        # телефоны просто пересобираем заново из строки через запятую
        for phone in list(person.phones):
            database.db_session.delete(phone)
        phones = [p.strip() for p in request.form.get("phones", "").split(",") if p.strip()]
        for number in phones:
            database.db_session.add(Phone(person_id=person.id, number=number))

        database.db_session.commit()
        flash(_("Изменения сохранены."), "success")
        return redirect(url_for("persons.detail", person_id=person.id))

    return render_template("persons/form.html", person=person)


@bp.route("/<int:person_id>/account/create", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def create_account(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)

    existing = database.db_session.query(User).filter_by(person_id=person.id).first()
    if existing:
        flash(_("У этого человека уже есть учётная запись."), "warning")
        return redirect(url_for("persons.detail", person_id=person.id))

    username = request.form["username"].strip()
    password = request.form["password"]

    if database.db_session.query(User).filter_by(username=username).first():
        flash(_("Такой логин уже занят."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

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
    database.db_session.commit()
    flash(_("Учётная запись создана. Сообщите человеку логин и пароль."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/account/reset-password", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def reset_password(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        abort(404)

    user.password_hash = generate_password_hash(request.form["password"])
    database.db_session.commit()
    flash(_("Пароль обновлён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/account/change-username", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def change_username(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        abort(404)

    new_username = request.form["username"].strip()
    if not new_username:
        flash(_("Логин не может быть пустым."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

    conflict = database.db_session.query(User).filter(
        User.username == new_username, User.id != user.id
    ).first()
    if conflict:
        flash(_("Такой логин уже занят."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

    user.username = new_username
    database.db_session.commit()
    flash(_("Логин обновлён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/account/toggle-active", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def toggle_active(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        abort(404)

    user.is_active = not user.is_active
    database.db_session.commit()
    flash(_("Доступ включён.") if user.is_active else _("Доступ отключён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


# ---------------------------------------------------------------------------
# Управление учётными записями: привязка/отвязка к человеку (только председатель)
# ---------------------------------------------------------------------------

@bp.route("/accounts")
@roles_required(RoleEnum.CHAIRMAN)
def accounts_list():
    users = database.db_session.query(User).order_by(User.username).all()
    unlinked_persons = (
        database.db_session.query(Person)
        .outerjoin(User, User.person_id == Person.id)
        .filter(User.id.is_(None))
        .order_by(Person.full_name)
        .all()
    )
    return render_template("persons/accounts.html", users=users, unlinked_persons=unlinked_persons)


@bp.route("/accounts/<int:user_id>/link", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def link_account(user_id):
    user = database.db_session.get(User, user_id)
    if user is None:
        abort(404)
    if user.person_id is not None:
        flash(_("Эта учётная запись уже привязана к человеку — сначала отвяжите."), "warning")
        return redirect(url_for("persons.accounts_list"))

    person_id = request.form.get("person_id", type=int)
    person = database.db_session.get(Person, person_id) if person_id else None
    if person is None:
        flash(_("Выберите человека для привязки."), "danger")
        return redirect(url_for("persons.accounts_list"))

    existing = database.db_session.query(User).filter_by(person_id=person.id).first()
    if existing:
        flash(_("У этого человека уже есть другая учётная запись."), "warning")
        return redirect(url_for("persons.accounts_list"))

    user.person_id = person.id
    database.db_session.flush()
    _sync_user_role(person)  # роль подтягивается под флаги этого человека
    database.db_session.commit()
    flash(_("Учётная запись привязана."), "success")
    return redirect(url_for("persons.accounts_list"))


@bp.route("/accounts/<int:user_id>/unlink", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def unlink_account(user_id):
    user = database.db_session.get(User, user_id)
    if user is None:
        abort(404)
    user.person_id = None
    database.db_session.commit()
    flash(_("Учётная запись отвязана от человека."), "success")
    return redirect(url_for("persons.accounts_list"))
