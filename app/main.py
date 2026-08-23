from flask import Blueprint, render_template, redirect, url_for

import datetime as dt

from . import database
from .auth import login_required
from .permissions import is_board
from .models import Garage, Person, GeneralMeeting, AnnualReport
from .accounting import cooperative_balance
from .penalty import accrue_penalties
from .permissions import is_chairman
from .setup_wizard import wizard_status

bp = Blueprint("main", __name__)


@bp.route("/dashboard")
@login_required
def dashboard():
    # рядовой член кооператива не видит общую сводку по кооперативу —
    # для него "панель" это сразу список его гаражей (личный кабинет)
    if not is_board():
        return redirect(url_for("cabinet.garages"))

    # Тихий автопересчёт пени на сегодня при каждом заходе правления на
    # дашборд — см. penalty.view() и accounting.penalty для подробностей
    # про то, почему это дёшево при повторных вызовах.
    accrue_penalties(dt.date.today())

    stats = {
        "garages_count": database.db_session.query(Garage).count(),
        "persons_count": database.db_session.query(Person).count(),
        "last_meeting": database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).first(),
        "last_report": database.db_session.query(AnnualReport).order_by(AnnualReport.year.desc()).first(),
        "coop_balance": cooperative_balance(),
    }

    setup_status = wizard_status() if is_chairman() else None
    return render_template("dashboard.html", stats=stats, setup_status=setup_status)
