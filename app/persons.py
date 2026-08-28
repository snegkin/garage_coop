import json
import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from . import database
from . import audit
from .i18n import translate as _
from .auth import login_required, roles_required, is_safe_next_url
from .permissions import is_board, sync_user_role
from .models import Person, Phone, User, RoleEnum, MemberAccount, PersonDataRevision, PersonDataRevisionStatus
from .accounting import balance

bp = Blueprint("persons", __name__, url_prefix="/persons")


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_persons():
    q = request.args.get("q", "").strip()
    show_pending_only = request.args.get("pending") == "1"
    query = database.db_session.query(Person)
    if q:
        query = query.filter(Person.full_name.ilike(f"%{q}%"))
    persons = query.order_by(Person.full_name).all()

    # Находим всех, у кого есть pending-ревизии
    all_revisions = (
        database.db_session.query(PersonDataRevision)
        .filter_by(status=PersonDataRevisionStatus.PENDING)
        .order_by(PersonDataRevision.submitted_at.desc())
        .all()
    )
    pending_by_person: dict[int, PersonDataRevision] = {}
    for rev in all_revisions:
        if rev.person_id not in pending_by_person:
            pending_by_person[rev.person_id] = rev

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

    # Если фильтр — только pending
    if show_pending_only:
        persons = [p for p in persons if p.id in pending_by_person]

    return render_template("persons/list.html", persons=persons, q=q, balances=balances, pending_by_person=pending_by_person, show_pending_only=show_pending_only)


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


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        # Получаем имя из формы (предполагается, что поле называется full_name)
        form_name = request.form.get("full_name", "").strip()

        # 1. Проверяем, есть ли уже такой человек
        existing_person = database.db_session.query(Person).filter(
            Person.full_name.ilike(form_name)
        ).first()

        if existing_person:
            flash(f"Человек с именем «{form_name}» уже существует в базе.", "danger")
            # Возвращаем пользователя обратно на форму
            return render_template("persons/form.html", person=None)

        person = Person(full_name="")
        _save_from_form(person, request.form)
        database.db_session.add(person)

        try:
            database.db_session.flush()

            phones = [p.strip() for p in request.form.get("phones", "").split(",") if p.strip()]
            for number in phones:
                database.db_session.add(Phone(person_id=person.id, number=number))

            database.db_session.commit()
            flash(_("Человек «{name}» добавлен.", name=person.full_name), "success")

        except IntegrityError:
            # 2. Перехватываем ошибку, если кто-то успел создать запись в эту же миллисекунду
            database.db_session.rollback()
            flash("Произошла ошибка при сохранении (возможно, такой человек уже существует).", "danger")
            return render_template("persons/form.html", person=None)

        next_url = request.form.get("next")
        if is_safe_next_url(next_url):
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
        return redirect(url_for("persons.list_persons") if is_board() else url_for("cabinet.profile"))
    if not is_board() and g.user.person_id != person_id:
        abort(403)
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
    audit.record(
        "account.password_reset", entity_type="user", entity_id=user.id,
        summary=f"Правление сбросило пароль пользователю «{user.username}» ({person.full_name})",
    )
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

    old_username = user.username
    user.username = new_username
    audit.record(
        "account.username_change", entity_type="user", entity_id=user.id,
        summary=f"Логин «{old_username}» ({person.full_name}) изменён на «{new_username}»",
    )
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
    audit.record(
        "account.toggle_active", entity_type="user", entity_id=user.id,
        summary=f"Доступ пользователю «{user.username}» ({person.full_name}) {'включён' if user.is_active else 'отключён'}",
    )
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
    sync_user_role(person)  # роль подтягивается под флаги этого человека
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


# ---------------------------------------------------------------------------
# Одобрение / отклонение изменений персональных данных (только председатель)
# ---------------------------------------------------------------------------

def _apply_revision(revision):
    """Применяет одобренные данные из ревизии к карточке Person."""
    person = database.db_session.get(Person, revision.person_id)
    if person is None:
        return
    try:
        snap = json.loads(revision.fields_snapshot)
    except Exception:
        return
    person.email = snap.get("email")
    person.telegram = snap.get("telegram")
    person.registration_address = snap.get("registration_address")
    person.residence_address = snap.get("residence_address")
    # телефоны
    for phone in list(person.phones):
        database.db_session.delete(phone)
    for number in snap.get("phones", []):
        database.db_session.add(Phone(person_id=person.id, number=number))
    # паспорт
    person.passport_series = snap.get("passport_series")
    person.passport_number = snap.get("passport_number")
    person.passport_issued_by = snap.get("passport_issued_by")
    person.passport_department_code = snap.get("passport_department_code")
    issue_date_str = snap.get("passport_issue_date")
    if issue_date_str:
        try:
            person.passport_issue_date = dt.date.fromisoformat(issue_date_str)
        except (ValueError, TypeError):
            pass
    else:
        person.passport_issue_date = None


@bp.route("/persons/<int:person_id>/revisions/approve/<int:revision_id>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def approve_revision(person_id, revision_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    revision = database.db_session.get(PersonDataRevision, revision_id)
    if revision is None or revision.person_id != person_id:
        abort(404)
    if revision.status != PersonDataRevisionStatus.PENDING:
        flash(_("Эта ревизия уже обработана."), "warning")
        return redirect(url_for("persons.list_persons"))

    _apply_revision(revision)
    revision.status = PersonDataRevisionStatus.APPROVED
    revision.reviewed_at = dt.datetime.utcnow()
    revision.reviewer_user_id = g.user.id
    database.db_session.commit()
    flash(_("Изменения для «{name}» одобрены и применены.", name=person.full_name), "success")
    return redirect(url_for("persons.list_persons"))


@bp.route("/persons/<int:person_id>/revisions/reject/<int:revision_id>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def reject_revision(person_id, revision_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    revision = database.db_session.get(PersonDataRevision, revision_id)
    if revision is None or revision.person_id != person_id:
        abort(404)
    if revision.status != PersonDataRevisionStatus.PENDING:
        flash(_("Эта ревизия уже обработана."), "warning")
        return redirect(url_for("persons.list_persons"))

    revision.status = PersonDataRevisionStatus.REJECTED
    revision.reviewed_at = dt.datetime.utcnow()
    revision.reviewer_user_id = g.user.id
    database.db_session.commit()
    flash(_("Изменения для «{name}» отклонены.", name=person.full_name), "info")
    return redirect(url_for("persons.list_persons"))
