"""
Личный кабинет: раздел, где любой залогиненный пользователь может посмотреть
и обновить свои актуальные контактные данные и данные своего гаража —
без обращения к правлению. Официальные/реестровые поля (паспорт, членство,
кадастровые номера, площадь и т.д.) здесь не редактируются — только
контактная информация, которая должна оставаться актуальной.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g

from . import database
from .auth import login_required
from .i18n import translate as _
from .models import Person, Phone, GarageOwnership, MemberAccount

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
        person.email = f.get("email") or None
        person.telegram = f.get("telegram") or None
        person.registration_address = f.get("registration_address") or None
        person.residence_address = f.get("residence_address") or None

        for phone in list(person.phones):
            database.db_session.delete(phone)
        phones = [p.strip() for p in f.get("phones", "").split(",") if p.strip()]
        for number in phones:
            database.db_session.add(Phone(person_id=person.id, number=number))

        database.db_session.commit()
        flash(_("Данные обновлены."), "success")
        return redirect(url_for("cabinet.profile"))

    return render_template("cabinet/profile.html", person=person)


@bp.route("/garages")
@login_required
def garages():
    person = _current_person()
    ownerships = []
    member_accounts_by_garage = {}
    if person is not None:
        ownerships = (
            database.db_session.query(GarageOwnership)
            .filter_by(person_id=person.id)
            .all()
        )
        accounts = database.db_session.query(MemberAccount).filter_by(person_id=person.id).all()
        for acc in accounts:
            member_accounts_by_garage.setdefault(acc.garage_id, []).append(acc)
    return render_template(
        "cabinet/garages.html", person=person, ownerships=ownerships,
        member_accounts_by_garage=member_accounts_by_garage,
    )
