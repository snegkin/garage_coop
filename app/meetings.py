import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required, is_safe_next_url
from .models import GeneralMeeting, Person, Document, DocumentType, RoleEnum
from .uploads import save_upload

bp = Blueprint("meetings", __name__, url_prefix="/meetings")


@bp.route("/")
@login_required
def list_meetings():
    meetings = database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).all()
    persons = database.db_session.query(Person).order_by(Person.full_name).all()
    return render_template("meetings/list.html", meetings=meetings, persons=persons)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        meeting_date = dt.date.fromisoformat(f["date"])

        protocol_document_id = None
        file_path = save_upload(request.files.get("protocol_file"), current_app.config["UPLOAD_FOLDER"])
        if file_path:
            protocol_doc = Document(
                doc_type=DocumentType.PROTOCOL,
                date=meeting_date,
                title=_("Протокол собрания от {date}", date=meeting_date.isoformat()),
                file_path=file_path,
            )
            database.db_session.add(protocol_doc)
            database.db_session.flush()
            protocol_document_id = protocol_doc.id

        meeting = GeneralMeeting(
            date=meeting_date,
            agenda=f.get("agenda") or None,
            is_annual_report_meeting=bool(f.get("is_annual_report_meeting")),
            secretary_person_id=int(f["secretary_person_id"]) if f.get("secretary_person_id") else None,
            chairman_person_id=int(f["chairman_person_id"]) if f.get("chairman_person_id") else None,
            protocol_document_id=protocol_document_id,
        )
        database.db_session.add(meeting)
        database.db_session.commit()
        flash(_("Собрание добавлено."), "success")

        next_url = request.form.get("next")
        if is_safe_next_url(next_url):
            return redirect(next_url)
        return redirect(url_for("meetings.list_meetings"))

    persons = database.db_session.query(Person).order_by(Person.full_name).all()
    return render_template("meetings/form.html", persons=persons)


@bp.route("/<int:meeting_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit(meeting_id):
    meeting = database.db_session.get(GeneralMeeting, meeting_id)
    if meeting is None:
        flash(_("Собрание не найдено."), "warning")
        return redirect(url_for("meetings.list_meetings"))

    f = request.form
    meeting.date = dt.date.fromisoformat(f["date"])
    meeting.agenda = f.get("agenda") or None
    meeting.is_annual_report_meeting = bool(f.get("is_annual_report_meeting"))
    meeting.secretary_person_id = int(f["secretary_person_id"]) if f.get("secretary_person_id") else None
    meeting.chairman_person_id = int(f["chairman_person_id"]) if f.get("chairman_person_id") else None

    file_path = save_upload(request.files.get("protocol_file"), current_app.config["UPLOAD_FOLDER"])
    if file_path:
        protocol_doc = Document(
            doc_type=DocumentType.PROTOCOL,
            date=meeting.date,
            title=_("Протокол собрания от {date}", date=meeting.date.isoformat()),
            file_path=file_path,
        )
        database.db_session.add(protocol_doc)
        database.db_session.flush()
        meeting.protocol_document_id = protocol_doc.id

    database.db_session.commit()
    flash(_("Собрание обновлено."), "success")
    return redirect(url_for("meetings.list_meetings"))
