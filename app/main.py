import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for
from sqlalchemy import func

from . import database
from .auth import login_required
from .permissions import is_board
from .models import (
    Garage, Person, GeneralMeeting, AnnualReport, MemberAccount, Charge, Payment, AuditLog, ChargeAllocation,
    PowerPhaseDevice, MailboxSettings,
)
from .accounting import cooperative_balance
from .permissions import is_chairman
from .setup_wizard import wizard_status
from . import mail_client
from .mail_client import MailError, DEFAULT_FOLDER

bp = Blueprint("main", __name__)

RECENT_ACTIVITY_LIMIT = 8  # последних записей журнала аудита на панели — сама панель, не замена /governance/audit-log
MAIL_PREVIEW_LIMIT = 5  # последних писем во входящих для виджета на панели — сама панель, не замена /mailbox/


def _mail_preview() -> dict:
    """Последние письма входящих для виджета на панели — живое IMAP/POP3-
    подключение, как и у самой /mailbox/ (см. mailbox.py), без кэширования
    в БД. При недоступности почты или отсутствии настройки виджет просто
    показывает соответствующее сообщение, не ломая дашборд целиком (панель
    открывают часто, отдельная письмовая подсистема не должна валить её при
    временной недоступности почтового сервера)."""
    settings = database.db_session.query(MailboxSettings).first()
    is_configured = bool(settings and settings.incoming_host and settings.username and settings.password_encrypted)
    if not is_configured:
        return {"is_configured": False, "messages": None, "error": None}

    try:
        with mail_client.get_incoming_client(settings) as client:
            page = client.list_messages(page=1, page_size=MAIL_PREVIEW_LIMIT, folder=DEFAULT_FOLDER)
    except MailError as exc:
        return {"is_configured": True, "messages": None, "error": str(exc)}

    return {"is_configured": True, "messages": page.messages, "error": None}


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


def _collection_rate(year: int) -> Decimal | None:
    """
    % собираемости за год — какая доля начисленного за год (по активным
    счетам членов) уже реально оплачена. Считается точно через
    ChargeAllocation (разнесение платежей по начислениям FIFO, см.
    accounting.reallocate_member_charges), а не через общий баланс счёта,
    который мог получить оплату вперемешку за разные годы. None, если за
    этот год ничего не начислялось — делить не на что.
    """
    total_charged = (
        database.db_session.query(func.sum(Charge.amount))
        .join(MemberAccount, Charge.account_id == MemberAccount.id)
        .filter(MemberAccount.is_archived.is_(False), Charge.year == year)
        .scalar()
    ) or Decimal("0")
    if total_charged == 0:
        return None
    total_paid = (
        database.db_session.query(func.sum(ChargeAllocation.amount))
        .join(Charge, ChargeAllocation.charge_id == Charge.id)
        .join(MemberAccount, Charge.account_id == MemberAccount.id)
        .filter(MemberAccount.is_archived.is_(False), Charge.year == year)
        .scalar()
    ) or Decimal("0")
    return (total_paid / total_charged * 100).quantize(Decimal("0.1"))


@bp.route("/dashboard")
@login_required
def dashboard():
    # рядовой член кооператива не видит общую сводку по кооперативу —
    # для него "панель" это сразу список его гаражей (личный кабинет)
    if not is_board():
        return redirect(url_for("cabinet.garages"))

    current_year = dt.date.today().year
    # Собираемость — за текущий год и 2 предыдущих (3 года), только те, где
    # вообще были начисления (_collection_rate возвращает None, если начислять
    # было нечего, — год просто пропускается, а не показывается нулём).
    collection_rates = [
        (year, rate)
        for year in (current_year, current_year - 1, current_year - 2)
        for rate in [_collection_rate(year)]
        if rate is not None
    ]
    stats = {
        "garages_count": database.db_session.query(Garage).count(),
        "persons_count": database.db_session.query(Person).count(),
        "last_meeting": database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).first(),
        "last_report": database.db_session.query(AnnualReport).order_by(AnnualReport.year.desc()).first(),
        "coop_balance": cooperative_balance(),
        "collection_rates": collection_rates,
        **_debt_summary(),
    }

    recent_activity = (
        database.db_session.query(AuditLog)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(RECENT_ACTIVITY_LIMIT)
        .all()
    )

    setup_status = wizard_status() if is_chairman() else None

    # Для виджета мониторинга электроэнергии на панели — только список
    # устройств (id/label), сами данные графика подтягивает JS с
    # electricity_monitor.history_data (см. dashboard.html), чтобы не
    # дублировать здесь его логику формирования точек.
    electricity_devices = (
        database.db_session.query(PowerPhaseDevice)
        .order_by(PowerPhaseDevice.sort_order, PowerPhaseDevice.id)
        .all()
    )

    return render_template(
        "dashboard.html", stats=stats, recent_activity=recent_activity, setup_status=setup_status,
        electricity_devices=electricity_devices, mail_preview=_mail_preview(),
    )
