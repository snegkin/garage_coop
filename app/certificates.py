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
from .accounting import balance
from .applications import sanitize_html, has_text, MAX_HTML_LENGTH
from .auth import login_required, roles_required
from .i18n import translate as _
from .legal_docs import _render_pdf_or_fallback, _coop_and_chairman
from .permissions import is_board
from .models import (
    CertificateCounter, Garage, GarageCertificate, GarageOwnership, MemberAccount, Person, RoleEnum,
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
    return render_template("certificates/list.html", certificates=certificates)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create():
    ownerships = sorted(
        database.db_session.query(GarageOwnership).all(),
        key=lambda o: (o.person.full_name.lower(), o.garage.number),
    )
    form = request.form if request.method == "POST" else request.args
    owner = form.get("owner")
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
                                issued_at=issued_at or dt.date.today())

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
        database.db_session.commit()
        flash(_("Справка № {number} выдана.", number=certificate.number), "success")
        return redirect(url_for("certificates.detail", certificate_id=certificate.id))

    return _render_form(ownerships, ownership, coop, chairman)


def _render_form(ownerships, ownership, coop, chairman, body_html=None, issued_at=None):
    debt = None
    if ownership is not None:
        debt = debt_total(ownership.person, ownership.garage)
        if body_html is None:
            body_html = default_body(ownership.person, ownership.garage, ownership, coop)
    return render_template(
        "certificates/form.html", ownerships=ownerships, ownership=ownership, coop=coop, chairman=chairman,
        body_html=body_html or "", issued_at=issued_at or dt.date.today(), debt=debt,
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
