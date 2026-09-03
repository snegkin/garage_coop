from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for
from sqlalchemy import func

from . import database
from .auth import login_required
from .permissions import is_board
from .models import Garage, Person, GeneralMeeting, AnnualReport, MemberAccount, Charge, Payment, AuditLog
from .accounting import cooperative_balance
from .permissions import is_chairman
from .setup_wizard import wizard_status

bp = Blueprint("main", __name__)

RECENT_ACTIVITY_LIMIT = 8  # последних записей журнала аудита на панели — сама панель, не замена /governance/audit-log


def _debt_summary() -> dict:
    """
    Сумма долга и число счетов с отрицательным балансом — по ВСЕМ активным
    лицевым счетам членов (взносы/налог/пеня вместе, тот же знак, что и
    accounting.balance: отрицательное = долг). Разом, двумя агрегатными
    запросами (GROUP BY account_id), а не циклом с balance() по каждому
    счёту — на дашборде, который открывают часто, это не должно стоить
    отдельного запроса на каждый лицевой счёт кооператива.
    """
    charged_by_account = dict(
        database.db_session.query(Charge.account_id, func.sum(Charge.amount))
        .join(MemberAccount, Charge.account_id == MemberAccount.id)
        .filter(MemberAccount.is_archived.is_(False))
        .group_by(Charge.account_id)
        .all()
    )
    paid_by_account = dict(
        database.db_session.query(Payment.account_id, func.sum(Payment.amount))
        .join(MemberAccount, Payment.account_id == MemberAccount.id)
        .filter(MemberAccount.is_archived.is_(False))
        .group_by(Payment.account_id)
        .all()
    )
    total_debt = Decimal("0")
    accounts_in_debt = 0
    for account_id, charged in charged_by_account.items():
        paid = paid_by_account.get(account_id, Decimal("0"))
        account_balance = paid - charged
        if account_balance < 0:
            total_debt += account_balance
            accounts_in_debt += 1
    return {"total_debt": total_debt, "accounts_in_debt": accounts_in_debt}


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
        **_debt_summary(),
    }

    recent_activity = (
        database.db_session.query(AuditLog)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(RECENT_ACTIVITY_LIMIT)
        .all()
    )

    setup_status = wizard_status() if is_chairman() else None
    return render_template("dashboard.html", stats=stats, recent_activity=recent_activity, setup_status=setup_status)
