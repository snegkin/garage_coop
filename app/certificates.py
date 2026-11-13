"""
Запросы справок о владении гаражом из личного кабинета (`/certificates/my`).

Сама справка — обычный документ раздела «Бланки и шаблоны»
(app/documents.py, шаблон с code="garage_certificate", составляет только
председатель); отдельного раздела и журнала у справок нет — `/certificates/`
и `/certificates/new` перенаправляют туда (старые ссылки, кнопки на
карточках гаража и человека).

Собственник запрашивает справку по своему гаражу (CertificateRequest):
председателю — уведомление и бейдж; оформление справки по запросу
(documents.create?request_id=…) переводит запрос в «готова — заберите у
председателя» с уведомлением собственнику; после выдачи на руки
председатель отмечает handed_over. Можно отказать с причиной
(reject_request) — тоже с уведомлением.
"""
import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort

from . import audit
from . import database
from . import notifications
from .auth import login_required, roles_required
from .i18n import translate as _
from .models import CertificateRequest, CertificateRequestStatus, GarageOwnership, RoleEnum

bp = Blueprint("certificates", __name__, url_prefix="/certificates")


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_certificates():
    return redirect(url_for("documents.index", _anchor="certificate-requests"))


@bp.route("/new")
@roles_required(RoleEnum.CHAIRMAN)
def create():
    """Кнопки «Справка о владении» на карточках гаража/человека —
    в составление документа по шаблону справки."""
    from .documents import certificate_template
    template = certificate_template()
    database.db_session.commit()
    args = {k: v for k, v in request.args.items() if k in ("owner", "person_id", "garage_id")}
    return redirect(url_for("documents.create", template_id=template.id, **args))


REQUEST_STATUS_BADGES = {
    CertificateRequestStatus.REQUESTED: ("запрошена", "text-bg-warning"),
    CertificateRequestStatus.READY: ("готова — заберите у председателя", "text-bg-success"),
    CertificateRequestStatus.HANDED_OVER: ("выдана на руки", "text-bg-secondary"),
    CertificateRequestStatus.REJECTED: ("отказано", "text-bg-danger"),
}


@bp.app_template_global("certificate_request_badge")
def request_badge(req: CertificateRequest) -> tuple[str, str]:
    text, css = REQUEST_STATUS_BADGES[req.status]
    return _(text), css


def _own_ownerships() -> list[GarageOwnership]:
    if g.user.person_id is None:
        return []
    return sorted(
        database.db_session.query(GarageOwnership).filter_by(person_id=g.user.person_id).all(),
        key=lambda o: o.garage.number,
    )


def notify_requester(req: CertificateRequest, subject: str, body: str) -> None:
    user = notifications.user_for_person(req.person_id)
    if user is not None:
        link = request.host_url.rstrip("/") + url_for("certificates.my_certificates")
        notifications.after_commit(notifications.notify_user_id, user.id, "my_requests", subject, f"{body}\n\n{link}")


@bp.route("/my", methods=["GET", "POST"])
@login_required
def my_certificates():
    """Личный кабинет: запросить справку по своему гаражу, статусы своих
    запросов и выданные справки."""
    ownerships = _own_ownerships()
    if request.method == "POST":
        ownership = next((o for o in ownerships if str(o.garage_id) == request.form.get("garage_id")), None)
        if ownership is None:
            flash(_("Справку можно запросить только по своему гаражу."), "danger")
            return redirect(url_for("certificates.my_certificates"))
        pending = (
            database.db_session.query(CertificateRequest)
            .filter_by(person_id=ownership.person_id, garage_id=ownership.garage_id)
            .filter(CertificateRequest.status.in_([CertificateRequestStatus.REQUESTED, CertificateRequestStatus.READY]))
            .first()
        )
        if pending is not None:
            flash(_("По этому гаражу справка уже запрошена — дождитесь её."), "warning")
            return redirect(url_for("certificates.my_certificates"))
        req = CertificateRequest(
            person_id=ownership.person_id, garage_id=ownership.garage_id, garage_number=ownership.garage.number,
            comment=(request.form.get("comment") or "").strip()[:1000] or None, created_at=dt.datetime.now(),
        )
        database.db_session.add(req)
        database.db_session.flush()
        audit.record(
            "certificate.request", entity_type="certificate_request", entity_id=req.id,
            summary=f"{ownership.person.full_name} запросил(а) справку о владении гаражом № {req.garage_number}"
                    + (f" ({req.comment})" if req.comment else ""),
        )
        database.db_session.commit()
        flash(_("Запрос отправлен председателю. Когда справка будет готова, придёт уведомление."), "success")
        return redirect(url_for("certificates.my_certificates"))

    requests_ = []
    certificates = []
    if g.user.person_id is not None:
        requests_ = (
            database.db_session.query(CertificateRequest).filter_by(person_id=g.user.person_id)
            .order_by(CertificateRequest.created_at.desc()).all()
        )
        from .documents import CERTIFICATE_CODE, visible_documents
        certificates = visible_documents(person_id=g.user.person_id, code=CERTIFICATE_CODE)
    return render_template(
        "certificates/my.html", ownerships=ownerships, requests=requests_, certificates=certificates,
        preselect=request.args.get("garage_id", type=int),
    )


def _get_request_or_404(request_id: int) -> CertificateRequest:
    req = database.db_session.get(CertificateRequest, request_id)
    if req is None:
        abort(404)
    return req


@bp.route("/requests/<int:request_id>/handed-over", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def request_handed_over(request_id):
    req = _get_request_or_404(request_id)
    if req.status != CertificateRequestStatus.READY:
        flash(_("Отметить выдачу можно только у готовой справки."), "warning")
        return redirect(url_for("documents.index", _anchor="certificate-requests"))
    req.status = CertificateRequestStatus.HANDED_OVER
    req.handed_over_at = dt.datetime.now()
    audit.record(
        "certificate.handed_over", entity_type="certificate_request", entity_id=req.id,
        summary=f"Справка о владении гаражом № {req.garage_number} выдана на руки: {req.person.full_name}",
    )
    database.db_session.commit()
    flash(_("Отмечено: справка выдана на руки."), "success")
    return redirect(url_for("documents.index", _anchor="certificate-requests"))


@bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def reject_request(request_id):
    req = _get_request_or_404(request_id)
    if req.status != CertificateRequestStatus.REQUESTED:
        flash(_("По этому запросу решение уже принято."), "warning")
        return redirect(url_for("documents.index", _anchor="certificate-requests"))
    req.status = CertificateRequestStatus.REJECTED
    req.reject_reason = (request.form.get("reason") or "").strip()[:1000] or None
    audit.record(
        "certificate.request_reject", entity_type="certificate_request", entity_id=req.id,
        summary=f"Отказано в справке о владении гаражом № {req.garage_number}: {req.person.full_name}"
                + (f" ({req.reject_reason})" if req.reject_reason else ""),
    )
    notify_requester(
        req, f"Справка по гаражу № {req.garage_number}: отказано",
        "Председатель отказал в выдаче справки о владении гаражом."
        + (f" Причина: {req.reject_reason}" if req.reject_reason else ""),
    )
    database.db_session.commit()
    flash(_("Запрос отклонён."), "success")
    return redirect(url_for("documents.index", _anchor="certificate-requests"))
