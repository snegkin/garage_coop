"""
Годовой отчёт председателя (`/annual-reports/`) — отчёт по расходам,
финансовая отчётность и ставки взносов на следующий год, составляется на
конкретном годовом отчётном собрании (GeneralMeeting.is_annual_report_meeting).
Одно такое собрание — один отчёт (AnnualReport.meeting_id уникален, см.
app/models.py). Создать/изменить отчёт может только председатель — сам
отчёт, как и список собраний, виден любому вошедшему пользователю
(прозрачность для членов кооператива).
"""
import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from werkzeug.utils import secure_filename

from . import database
from . import audit
from .i18n import translate as _, parse_optional_decimal
from .auth import login_required, roles_required
from .models import AnnualReport, FeeRate, FeeType, GeneralMeeting, Document, DocumentType, RoleEnum
from .uploads import save_upload

bp = Blueprint("annual_reports", __name__, url_prefix="/annual-reports")


def _rated_fee_types():
    """Виды взноса, на которые имеет смысл утверждать ставку — те же, что
    предлагаются в finance.mass_charge (с type_code, не пеня): земельный
    налог, членский взнос, целевой взнос и т.п."""
    return database.db_session.query(FeeType).filter(
        FeeType.type_code.isnot(None), FeeType.is_penalty.is_(False)
    ).order_by(FeeType.name).all()


def _save_report_document(file_storage, doc_type: DocumentType, title: str) -> int | None:
    """Сохраняет файл как новый Document (см. app/meetings.py: create() —
    тот же приём: отчётный документ создаётся прямо инлайн из формы
    отчёта, отдельного экрана «прикрепить документ» для этого не заводим).
    Возвращает id нового документа или None, если файл не выбран."""
    if not file_storage or not file_storage.filename:
        return None
    file_path = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"])
    if not file_path:
        return None
    doc = Document(
        doc_type=doc_type,
        date=dt.date.today(),
        title=title,
        file_path=file_path,
        file_name=secure_filename(file_storage.filename),
    )
    database.db_session.add(doc)
    database.db_session.flush()
    return doc.id


def _apply_fee_rates(report: AnnualReport, form) -> None:
    """Приводит FeeRate отчёта в соответствие с формой: по каждому виду
    взноса, где заполнено хотя бы одно из полей, — создаёт или обновляет
    строку; где оба поля пусты — удаляет (если была). Пустые строки не
    хранятся — не захламлять таблицу нулями за виды взноса, по которым
    правление ставку в этом году не меняло."""
    existing = {rate.fee_type_id: rate for rate in report.fee_rates}
    for fee_type in _rated_fee_types():
        rate_per_sqm = parse_optional_decimal(form.get(f"rate_per_sqm_{fee_type.id}"))
        fixed_amount = parse_optional_decimal(form.get(f"fixed_amount_{fee_type.id}"))
        rate = existing.get(fee_type.id)
        if rate_per_sqm is None and fixed_amount is None:
            if rate is not None:
                database.db_session.delete(rate)
            continue
        if rate is None:
            rate = FeeRate(annual_report=report, fee_type_id=fee_type.id)
            database.db_session.add(rate)
        rate.rate_per_sqm = rate_per_sqm
        rate.fixed_amount = fixed_amount


@bp.route("/")
@login_required
def list_reports():
    reports = database.db_session.query(AnnualReport).order_by(AnnualReport.year.desc()).all()
    return render_template("annual_reports/list.html", reports=reports)


@bp.route("/<int:report_id>")
@login_required
def view(report_id):
    report = database.db_session.get(AnnualReport, report_id)
    if report is None:
        abort(404)
    return render_template("annual_reports/view.html", report=report)


@bp.route("/new/<int:meeting_id>", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create(meeting_id):
    meeting = database.db_session.get(GeneralMeeting, meeting_id)
    if meeting is None or not meeting.is_annual_report_meeting:
        abort(404)
    if meeting.annual_report is not None:
        return redirect(url_for("annual_reports.edit", report_id=meeting.annual_report.id))

    if request.method == "POST":
        f = request.form
        year = int(f.get("year") or meeting.date.year)
        report = AnnualReport(
            year=year,
            meeting_id=meeting.id,
            budget_total=parse_optional_decimal(f.get("budget_total")),
            budget_approved=bool(f.get("budget_approved")),
            comment=f.get("comment") or None,
        )
        report.spending_report_document_id = _save_report_document(
            request.files.get("spending_report_file"), DocumentType.REPORT,
            _("Отчёт о расходах за {year} год", year=year),
        )
        report.accounting_report_document_id = _save_report_document(
            request.files.get("accounting_report_file"), DocumentType.REPORT,
            _("Финансовая отчётность за {year} год", year=year),
        )
        database.db_session.add(report)
        database.db_session.flush()
        _apply_fee_rates(report, f)
        audit.record(
            "annual_report.create", entity_type="annual_report", entity_id=report.id,
            summary=f"Составлен годовой отчёт за {year} год",
        )
        database.db_session.commit()
        flash(_("Годовой отчёт сохранён."), "success")
        return redirect(url_for("annual_reports.view", report_id=report.id))

    return render_template(
        "annual_reports/form.html", report=None, meeting=meeting,
        fee_types=_rated_fee_types(), rates_by_fee_type={},
    )


@bp.route("/<int:report_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit(report_id):
    report = database.db_session.get(AnnualReport, report_id)
    if report is None:
        abort(404)

    if request.method == "POST":
        f = request.form
        report.year = int(f.get("year") or report.year)
        report.budget_total = parse_optional_decimal(f.get("budget_total"))
        report.budget_approved = bool(f.get("budget_approved"))
        report.comment = f.get("comment") or None

        new_spending_id = _save_report_document(
            request.files.get("spending_report_file"), DocumentType.REPORT,
            _("Отчёт о расходах за {year} год", year=report.year),
        )
        if new_spending_id is not None:
            report.spending_report_document_id = new_spending_id
        new_accounting_id = _save_report_document(
            request.files.get("accounting_report_file"), DocumentType.REPORT,
            _("Финансовая отчётность за {year} год", year=report.year),
        )
        if new_accounting_id is not None:
            report.accounting_report_document_id = new_accounting_id

        _apply_fee_rates(report, f)
        audit.record(
            "annual_report.edit", entity_type="annual_report", entity_id=report.id,
            summary=f"Изменён годовой отчёт за {report.year} год",
        )
        database.db_session.commit()
        flash(_("Годовой отчёт сохранён."), "success")
        return redirect(url_for("annual_reports.view", report_id=report.id))

    rates_by_fee_type = {rate.fee_type_id: rate for rate in report.fee_rates}
    return render_template(
        "annual_reports/form.html", report=report, meeting=report.meeting,
        fee_types=_rated_fee_types(), rates_by_fee_type=rates_by_fee_type,
    )
