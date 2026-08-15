"""
Личный кабинет: раздел, где любой залогиненный пользователь может посмотреть
свои данные и предложить изменения контактной информации. Изменения
применяются только после одобрения председателем — см. PersonDataRevision.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
import json
import datetime as dt

from werkzeug.security import check_password_hash, generate_password_hash

from . import database
from .auth import login_required
from .i18n import translate as _
from .models import Person, Phone, GarageOwnership, MemberAccount, PersonDataRevision, PersonDataRevisionStatus, User
from .accounting import balance as account_balance

bp = Blueprint("cabinet", __name__, url_prefix="/cabinet")


def _current_person():
    if g.user.person_id is None:
        return None
    return database.db_session.get(Person, g.user.person_id)


@bp.route("/")
@login_required
def index():
    return redirect(url_for("cabinet.profile"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    person = _current_person()
    if person is None:
        flash(_("Ваша учётная запись пока не привязана к карточке члена кооператива. Обратитесь в правление."), "warning")
        return render_template("cabinet/profile.html", person=None)

    if request.method == "POST":
        f = request.form
        # Сохраняем текущие (одобренные) данные для сравнения
        current = {
            "email": person.email,
            "telegram": person.telegram,
            "registration_address": person.registration_address,
            "residence_address": person.residence_address,
            "phones": sorted([p.number for p in person.phones]),
            "passport_series": person.passport_series,
            "passport_number": person.passport_number,
            "passport_issued_by": person.passport_issued_by,
            "passport_issue_date": person.passport_issue_date.isoformat() if person.passport_issue_date else None,
            "passport_department_code": person.passport_department_code,
        }
        new_data = {
            "email": f.get("email") or None,
            "telegram": f.get("telegram") or None,
            "registration_address": f.get("registration_address") or None,
            "residence_address": f.get("residence_address") or None,
            "phones": sorted([p.strip() for p in f.get("phones", "").split(",") if p.strip()]),
            "passport_series": f.get("passport_series") or None,
            "passport_number": f.get("passport_number") or None,
            "passport_issued_by": f.get("passport_issued_by") or None,
            "passport_issue_date": f.get("passport_issue_date") or None,
            "passport_department_code": f.get("passport_department_code") or None,
        }
        # Если ничего не изменилось — предупреждаем
        if current == new_data:
            flash(_("Нет изменений для отправки."), "info")
            return redirect(url_for("cabinet.profile"))
        # Создаём ревизию — данные не применяются сразу
        revision = PersonDataRevision(
            person_id=person.id,
            submitted_by_user_id=g.user.id,
            fields_snapshot=json.dumps(new_data, ensure_ascii=False),
            status=PersonDataRevisionStatus.PENDING,
        )
        database.db_session.add(revision)
        database.db_session.commit()
        flash(_("Изменения отправлены на рассмотрение председателю."), "success")
        return redirect(url_for("cabinet.profile"))

    # Для GET: определяем, есть ли pending-ревизии у этого человека
    pending_revision = (
        database.db_session.query(PersonDataRevision)
        .filter_by(person_id=person.id, status=PersonDataRevisionStatus.PENDING)
        .order_by(PersonDataRevision.submitted_at.desc())
        .first()
    )
    snap = None
    if pending_revision:
        try:
            snap = json.loads(pending_revision.fields_snapshot)
        except Exception:
            snap = None

    # Если есть pending — подставляем данные из ревизии в форму
    display_person = person
    if snap:
        issue_date_str = snap.get("passport_issue_date")
        issue_date = None
        if issue_date_str:
            try:
                issue_date = dt.date.fromisoformat(issue_date_str)
            except (ValueError, TypeError):
                pass
        display_person = type('Person', (), {
            'id': person.id,
            'full_name': person.full_name,
            'email': snap.get('email'),
            'telegram': snap.get('telegram'),
            'registration_address': snap.get('registration_address'),
            'residence_address': snap.get('residence_address'),
            'phones': [Phone(id=-1, number=n) for n in snap.get('phones', [])],
            'passport_series': snap.get('passport_series'),
            'passport_number': snap.get('passport_number'),
            'passport_issued_by': snap.get('passport_issued_by'),
            'passport_issue_date': issue_date,
            'passport_department_code': snap.get('passport_department_code'),
            'membership_start_date': person.membership_start_date,
            'membership_end_date': person.membership_end_date,
            'comment': person.comment,
        })()

    return render_template("cabinet/profile.html", person=display_person, pending_revision=pending_revision, pending_data=snap)


@bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    f = request.form
    current_password = f.get("current_password", "")
    new_password = f.get("new_password", "")
    confirm_password = f.get("confirm_password", "")

    if not check_password_hash(g.user.password_hash, current_password):
        flash(_("Текущий пароль указан неверно."), "danger")
        return redirect(url_for("cabinet.profile"))
    if len(new_password) < 4:
        flash(_("Новый пароль слишком короткий (минимум 4 символа)."), "danger")
        return redirect(url_for("cabinet.profile"))
    if new_password != confirm_password:
        flash(_("Новый пароль и подтверждение не совпадают."), "danger")
        return redirect(url_for("cabinet.profile"))

    g.user.password_hash = generate_password_hash(new_password)
    database.db_session.commit()
    flash(_("Пароль изменён."), "success")
    return redirect(url_for("cabinet.profile"))


@bp.route("/garages")
@login_required
def garages():
    person = _current_person()
    ownerships = []
    member_accounts_by_garage = {}
    electricity_by_garage = {}
    if person is not None:
        ownerships = (
            database.db_session.query(GarageOwnership)
            .filter_by(person_id=person.id)
            .all()
        )
        accounts = database.db_session.query(MemberAccount).filter_by(person_id=person.id).all()
        for acc in accounts:
            member_accounts_by_garage.setdefault(acc.garage_id, []).append(acc)
        for o in ownerships:
            garage = o.garage
            if garage.account is not None:
                electricity_by_garage[garage.id] = (garage.account, account_balance(garage))
    return render_template(
        "cabinet/garages.html", person=person, ownerships=ownerships,
        member_accounts_by_garage=member_accounts_by_garage,
        electricity_by_garage=electricity_by_garage,
    )
