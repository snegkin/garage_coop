"""
Справки правления о владении гаражом (`/certificates/`) — для оформления
гаража в собственность по «гаражной амнистии» (ст. 3.7 Федерального закона
от 25.10.2001 № 137-ФЗ). Выдаёт председатель: выбирает собственника и
гараж, система подставляет стартовый текст по данным реестра (ФИО в
дательном падеже, паспорт, адрес регистрации, членство, номер/площадь/
кадастровый номер гаража, доля, отсутствие задолженности — только если
её действительно нет), председатель правит текст прямо на бланке (тот же
редактор, что у заявлений — applications/_editor.html) и выдаёт справку:
номер «<год>-<N>», текст сохраняется в журнале как выдан (повторная
печать — того же текста, не пересобранного по изменившимся данным).

Собственник может запросить справку из личного кабинета
(request_certificate, CertificateRequest): председателю — уведомление и
бейдж; оформление справки по запросу (create?request_id=…) переводит
запрос в «готова — заберите у председателя» с уведомлением собственнику;
после выдачи на руки председатель отмечает handed_over. Можно отказать
с причиной (reject_request) — тоже с уведомлением.

Ничего, чего система не знает (например, выплату паевого взноса), в
стартовый текст не утверждается — это председатель дописывает сам.
"""
import datetime as dt
from decimal import Decimal
from fractions import Fraction

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from markupsafe import escape

from . import audit
from . import database
from . import name_declension
from . import notifications
from .accounting import balance
from .applications import sanitize_html, has_text, MAX_HTML_LENGTH
from .auth import login_required, roles_required
from .i18n import translate as _
from .legal_docs import _render_pdf_or_fallback, _coop_and_chairman
from .permissions import is_board
from .models import (
    CertificateCounter, CertificateRequest, CertificateRequestStatus, Garage, GarageCertificate, GarageOwnership,
    MemberAccount, Person, RoleEnum,
)

bp = Blueprint("certificates", __name__, url_prefix="/certificates")


# ---------------------------------------------------------------------------
# Стартовый текст справки
# ---------------------------------------------------------------------------

def _share_text(share: Decimal) -> str:
    fraction = Fraction(share).limit_denominator(100)
    return f"{fraction.numerator}/{fraction.denominator}"


def debt_total(person: Person, garage: Garage) -> Decimal:
    """Задолженность человека перед кооперативом (сумма отрицательных
    балансов его действующих лицевых счетов + электричество по этому
    гаражу) — для фразы «задолженности не имеет» и предупреждения
    председателю."""
    total = Decimal("0")
    accounts = database.db_session.query(MemberAccount).filter_by(person_id=person.id, is_archived=False).all()
    for account in accounts:
        b = balance(account)
        if b < 0:
            total -= b
    if garage.account is not None:
        b = balance(garage)
        if b < 0:
            total -= b
    return total


def default_body(person: Person, garage: Garage, ownership: GarageOwnership, coop) -> str:
    # Краткое название («ГМ «Восход»») не склоняется, полное («Гаражно-
    # строительный кооператив …») в «на территории …» потребовало бы
    # родительного падежа — без краткого пишем просто «кооператива».
    coop_name = coop.short_name if coop and coop.short_name else "кооператива"
    pronoun = {"male": "он", "female": "она"}.get(name_declension.gender(person.full_name), "он(а)")

    details = []
    if person.birth_date:
        details.append(f"{person.birth_date.strftime('%d.%m.%Y')} г. р.")
    if person.passport_series or person.passport_number:
        passport = f"паспорт серия {person.passport_series or '—'} № {person.passport_number or '—'}"
        if person.passport_issue_date:
            passport += f", выдан {person.passport_issue_date.strftime('%d.%m.%Y')}"
        details.append(passport)
    address = person.registration_address or person.residence_address
    if address:
        details.append(f"адрес регистрации: {address}")
    who = escape(name_declension.dative(person.full_name))
    if details:
        who += escape(" (" + "; ".join(details) + ")")

    member = ""
    if person.membership_start_date and not person.membership_end_date:
        member = f"является членом {coop_name} с {person.membership_start_date.strftime('%d.%m.%Y')} и "
    if ownership.share == 1:
        garage_text, located = f"гаражом № {garage.number}", "расположенным"
    else:
        garage_text = f"{_share_text(ownership.share)} доли в праве на гараж № {garage.number}"
        located = "расположенный"
    location = f", {located} на территории {coop_name}"
    if coop and coop.legal_address:
        location += f" по адресу: {coop.legal_address}"
    facts = [f"площадью {str(garage.area_sqm).replace('.', ',')} м²"]
    if garage.cadastral_number:
        facts.append(f"кадастровый номер гаража {garage.cadastral_number}")
    if garage.land_cadastral_number:
        facts.append(f"кадастровый номер земельного участка {garage.land_cadastral_number}")

    paragraphs = [
        f"Выдана {who} в том, что {pronoun} "
        + escape(f"{member}владеет {garage_text}{location}, {', '.join(facts)}.")
    ]
    if debt_total(person, garage) == 0:
        paragraphs.append("Задолженности по взносам и платежам перед кооперативом не имеет.")
    paragraphs.append(
        "Справка выдана для предъявления в органы государственной власти и органы местного самоуправления "
        "при оформлении права собственности на гараж в соответствии со статьёй 3.7 Федерального закона "
        "от 25.10.2001 № 137-ФЗ «О введении в действие Земельного кодекса Российской Федерации»."
    )
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def next_number(today: dt.date) -> str:
    counter = database.db_session.get(CertificateCounter, today.year)
    if counter is None:
        counter = CertificateCounter(year=today.year, last_number=0)
        database.db_session.add(counter)
    counter.last_number += 1
    return f"{today.year}-{counter.last_number}"


def visible_certificates(person_id: int | None = None, garage_id: int | None = None) -> list[GarageCertificate]:
    """Для блоков на карточках человека/гаража — правлению все, члену — свои."""
    query = database.db_session.query(GarageCertificate)
    if person_id is not None:
        query = query.filter(GarageCertificate.person_id == person_id)
    if garage_id is not None:
        query = query.filter(GarageCertificate.garage_id == garage_id)
    if not is_board():
        query = query.filter(GarageCertificate.person_id == g.user.person_id)
    return query.order_by(GarageCertificate.issued_at.desc(), GarageCertificate.id.desc()).all()


def _get_or_404(certificate_id: int) -> GarageCertificate:
    certificate = database.db_session.get(GarageCertificate, certificate_id)
    if certificate is None:
        abort(404)
    if not (is_board() or (g.user.person_id is not None and g.user.person_id == certificate.person_id)):
        abort(403)
    return certificate


# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------

@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_certificates():
    certificates = (
        database.db_session.query(GarageCertificate)
        .order_by(GarageCertificate.issued_at.desc(), GarageCertificate.id.desc())
        .all()
    )
    open_requests = (
        database.db_session.query(CertificateRequest)
        .filter(CertificateRequest.status.in_([CertificateRequestStatus.REQUESTED, CertificateRequestStatus.READY]))
        .order_by(CertificateRequest.created_at)
        .all()
    )
    return render_template("certificates/list.html", certificates=certificates, open_requests=open_requests)


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


def _notify_requester(req: CertificateRequest, subject: str, body: str) -> None:
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
        certificates = (
            database.db_session.query(GarageCertificate).filter_by(person_id=g.user.person_id)
            .order_by(GarageCertificate.issued_at.desc()).all()
        )
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
        return redirect(url_for("certificates.list_certificates"))
    req.status = CertificateRequestStatus.HANDED_OVER
    req.handed_over_at = dt.datetime.now()
    audit.record(
        "certificate.handed_over", entity_type="certificate_request", entity_id=req.id,
        summary=f"Справка о владении гаражом № {req.garage_number} выдана на руки: {req.person.full_name}",
    )
    database.db_session.commit()
    flash(_("Отмечено: справка выдана на руки."), "success")
    return redirect(url_for("certificates.list_certificates"))


@bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def reject_request(request_id):
    req = _get_request_or_404(request_id)
    if req.status != CertificateRequestStatus.REQUESTED:
        flash(_("По этому запросу решение уже принято."), "warning")
        return redirect(url_for("certificates.list_certificates"))
    req.status = CertificateRequestStatus.REJECTED
    req.reject_reason = (request.form.get("reason") or "").strip()[:1000] or None
    audit.record(
        "certificate.request_reject", entity_type="certificate_request", entity_id=req.id,
        summary=f"Отказано в справке о владении гаражом № {req.garage_number}: {req.person.full_name}"
                + (f" ({req.reject_reason})" if req.reject_reason else ""),
    )
    _notify_requester(
        req, f"Справка по гаражу № {req.garage_number}: отказано",
        "Председатель отказал в выдаче справки о владении гаражом."
        + (f" Причина: {req.reject_reason}" if req.reject_reason else ""),
    )
    database.db_session.commit()
    flash(_("Запрос отклонён."), "success")
    return redirect(url_for("certificates.list_certificates"))


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create():
    ownerships = sorted(
        database.db_session.query(GarageOwnership).all(),
        key=lambda o: (o.person.full_name.lower(), o.garage.number),
    )
    form = request.form if request.method == "POST" else request.args
    owner = form.get("owner")
    cert_request = None
    if form.get("request_id"):
        cert_request = database.db_session.get(CertificateRequest, form.get("request_id", type=int))
        if cert_request is None or cert_request.status != CertificateRequestStatus.REQUESTED:
            flash(_("Запрос не найден или уже обработан."), "warning")
            return redirect(url_for("certificates.list_certificates"))
        owner = f"{cert_request.person_id}:{cert_request.garage_id}"
    if not owner and (form.get("person_id") or form.get("garage_id")):
        owner = next((
            f"{o.person_id}:{o.garage_id}" for o in ownerships
            if (not form.get("person_id") or str(o.person_id) == form.get("person_id"))
            and (not form.get("garage_id") or str(o.garage_id) == form.get("garage_id"))
        ), None)
    ownership = next((o for o in ownerships if f"{o.person_id}:{o.garage_id}" == owner), None)
    coop, chairman = _coop_and_chairman()

    if request.method == "POST":
        raw = form.get("body_html") or ""
        body_html = sanitize_html(raw)
        try:
            issued_at = dt.date.fromisoformat(form.get("issued_at") or "")
        except ValueError:
            issued_at = None
        error = None
        if ownership is None:
            error = _("Выберите собственника и гараж.")
        elif issued_at is None:
            error = _("Укажите дату справки.")
        elif len(raw) > MAX_HTML_LENGTH:
            error = _("Текст справки слишком длинный.")
        elif not has_text(body_html):
            error = _("Текст справки пуст.")
        if error:
            flash(error, "danger")
            return _render_form(ownerships, ownership, coop, chairman, body_html=body_html,
                                issued_at=issued_at or dt.date.today(), cert_request=cert_request)

        certificate = GarageCertificate(
            number=next_number(issued_at), person_id=ownership.person_id, garage_id=ownership.garage_id,
            garage_number=ownership.garage.number, body_html=body_html, issued_at=issued_at,
            issued_by_person_id=g.user.person_id or (chairman.id if chairman else None),
        )
        database.db_session.add(certificate)
        database.db_session.flush()
        audit.record(
            "certificate.issue", entity_type="certificate", entity_id=certificate.id,
            summary=f"Выдана справка № {certificate.number} о владении гаражом № {certificate.garage_number}: "
                    f"{ownership.person.full_name}",
        )
        if cert_request is not None:
            cert_request.status = CertificateRequestStatus.READY
            cert_request.certificate_id = certificate.id
            cert_request.ready_at = dt.datetime.now()
            _notify_requester(
                cert_request, f"Справка по гаражу № {cert_request.garage_number} готова",
                f"Справка № {certificate.number} о владении гаражом № {cert_request.garage_number} готова — "
                "заберите подписанный экземпляр у председателя.",
            )
        database.db_session.commit()
        flash(_("Справка № {number} выдана.", number=certificate.number), "success")
        return redirect(url_for("certificates.detail", certificate_id=certificate.id))

    if cert_request is not None and ownership is None:
        flash(_("Заявитель уже не числится собственником этого гаража."), "warning")
    return _render_form(ownerships, ownership, coop, chairman, cert_request=cert_request)


def _render_form(ownerships, ownership, coop, chairman, body_html=None, issued_at=None, cert_request=None):
    debt = None
    if ownership is not None:
        debt = debt_total(ownership.person, ownership.garage)
        if body_html is None:
            body_html = default_body(ownership.person, ownership.garage, ownership, coop)
    return render_template(
        "certificates/form.html", ownerships=ownerships, ownership=ownership, coop=coop, chairman=chairman,
        body_html=body_html or "", issued_at=issued_at or dt.date.today(), debt=debt, cert_request=cert_request,
    )


@bp.route("/<int:certificate_id>")
@login_required
def detail(certificate_id):
    certificate = _get_or_404(certificate_id)
    coop, _chairman = _coop_and_chairman()
    return render_template("certificates/detail.html", certificate=certificate, coop=coop)


@bp.route("/<int:certificate_id>/pdf")
@login_required
def pdf(certificate_id):
    certificate = _get_or_404(certificate_id)
    coop, _chairman = _coop_and_chairman()
    return _render_pdf_or_fallback(
        "certificates/pdf.html", f"certificate-{certificate.number}.pdf", "certificates/detail.html",
        certificate=certificate, coop=coop,
    )
