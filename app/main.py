from flask import Blueprint, render_template, redirect, url_for

from . import database
from .auth import login_required
from .permissions import is_board
from .models import Garage, Person, GeneralMeeting, AnnualReport
from .accounting import cooperative_balance

bp = Blueprint("main", __name__)


@bp.route("/dashboard")
@login_required
def dashboard():
    # рядовой член кооператива не видит общую сводку по кооперативу —
    # для него "панель" это сразу список его гаражей (личный кабинет)
    if not is_board():
        return redirect(url_for("cabinet.garages"))

    stats = {
        "garages_count": database.db_session.query(Garage).count(),
        "persons_count": database.db_session.query(Person).count(),
        "last_meeting": database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).first(),
        "last_report": database.db_session.query(AnnualReport).order_by(AnnualReport.year.desc()).first(),
        "coop_balance": cooperative_balance(),
    }
    return render_template("dashboard.html", stats=stats)
