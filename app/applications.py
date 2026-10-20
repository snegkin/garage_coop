"""
Заявления членов кооператива в правление (`/applications/`) — документ на
бланке: шапка с реквизитами кооператива (тот же бланк, что у уведомления о
задолженности и расчёта пени, legal_docs/_letterhead.html), справа —
«В правление …» и данные заявителя (ФИО, адрес, паспорт, телефон, гараж).
Текст пишется прямо на листе — редактор на contenteditable с выбором
шрифта/размера/выравнивания/списков (applications/_sheet_editor.html), не
textarea; на сервере HTML чистится bleach'ем (sanitize_html) — разрешена
только разметка документа и inline-стили оформления текста.

Организационная модель:
- Подать может собственник гаража (член кооператива), по одному из своих
  гаражей — заявление подшивается и к заявителю, и к гаражу.
- Вид: смена собственника гаража / официальная претензия / иное — от вида
  зависит заголовок («Заявление»/«Претензия») и стартовый текст.
- После подачи — уведомление правлению (журнал аудита, группа «Ждёт
  решения правления»). Видят заявление любой член правления и сам
  заявитель; открытие карточки членом правления отмечает его как
  ознакомившегося (ApplicationRead).
- Любой член правления может ответить (ответ — тоже документ на бланке
  кооператива, адресованный заявителю) или отклонить (причина
  необязательна); заявителю уходит уведомление (подписка notify_my_requests).
  Можно и ничего не делать — заявление остаётся «на рассмотрении».
- Поданное заявление не редактируется — это документ; реквизиты заявителя
  сохраняются на момент подачи (Application.applicant_*).
"""
import datetime as dt

import bleach
from bleach.css_sanitizer import CSSSanitizer
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort

from . import audit
from . import database
from . import notifications
from .i18n import translate as _
from .auth import login_required, roles_required
from .legal_docs import _render_pdf_or_fallback, _coop_and_chairman
from .permissions import is_board
from .models import (
    Application, ApplicationRead, ApplicationCounter, ApplicationKind, ApplicationStatus,
    Garage, GarageOwnership, Person, RoleEnum, short_name_of,
)

bp = Blueprint("applications", __name__, url_prefix="/applications")


# ---------------------------------------------------------------------------
# Виды заявлений и стартовый текст
# ---------------------------------------------------------------------------
# Сам текст документа не переводится через i18n — заявление ведётся на
# языке кооператива, как и остальные печатные формы (см. audit.record).

KIND_HEADINGS = {
    ApplicationKind.OWNERSHIP_CHANGE: "Заявление",
    ApplicationKind.CLAIM: "Претензия",
    ApplicationKind.OTHER: "Заявление",
}

# «Ответ на претензию», «Ответ на заявление» — заголовок ответа правления.
KIND_ACCUSATIVE = {
    ApplicationKind.OWNERSHIP_CHANGE: "заявление",
    ApplicationKind.CLAIM: "претензию",
    ApplicationKind.OTHER: "заявление",
}

DEFAULT_TITLES = {
    ApplicationKind.OWNERSHIP_CHANGE: "о смене собственника гаража",
    ApplicationKind.CLAIM: "",
    ApplicationKind.OTHER: "",
}

# {garage} — номер гаража, подставляется и в редакторе (при смене гаража,
# пока текст не трогали), и здесь — default_body().
DEFAULT_BODIES = {
    ApplicationKind.OWNERSHIP_CHANGE: (
        "<p>Прошу переоформить членство в кооперативе и лицевые счета по гаражу № {garage} "
        "на нового собственника — ____________________ (ФИО) в связи с ____________________ "
        "(продажей, дарением, наследованием и т.п.).</p>"
        "<p>Копии документов, подтверждающих переход права собственности на гараж, прилагаю.</p>"
        "<p>Приложения:</p><ol><li>&nbsp;</li></ol>"
    ),
    ApplicationKind.CLAIM: (
        "<p>Настоящим заявляю претензию по следующему вопросу: ____________________.</p>"
        "<p>Обстоятельства: ____________________.</p>"
        "<p>Требую: ____________________.</p>"
        "<p>Прошу рассмотреть претензию и дать письменный ответ в течение 30 дней.</p>"
    ),
    ApplicationKind.OTHER: "<p>Прошу ____________________.</p>",
}


def kind_label(kind: ApplicationKind) -> str:
    return {
        ApplicationKind.OWNERSHIP_CHANGE: _("Смена собственника гаража"),
        ApplicationKind.CLAIM: _("Официальная претензия"),
        ApplicationKind.OTHER: _("Иное заявление"),
    }[kind]


def application_heading(application: Application) -> str:
    """«Заявление о смене собственника гаража», «Претензия» — для списков и уведомлений."""
    heading = KIND_HEADINGS[application.kind]
    return f"{heading} {application.title}" if application.title else heading


def status_badge(application: Application) -> tuple[str, str]:
    """(текст, класс бейджа) — «на рассмотрении», пока кто-то из правления
    уже открыл заявление, но решения нет; до этого — «подано»."""
    if application.status == ApplicationStatus.ANSWERED:
        return _("ответ дан"), "text-bg-success"
    if application.status == ApplicationStatus.REJECTED:
        return _("отклонено"), "text-bg-danger"
    if application.reads:
        return _("на рассмотрении"), "text-bg-warning"
    return _("подано"), "text-bg-secondary"


# ---------------------------------------------------------------------------
# Санитизация HTML из редактора
# ---------------------------------------------------------------------------

ALLOWED_TAGS = [
    "p", "div", "br", "span", "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup",
    "ul", "ol", "li", "blockquote", "hr",
    "table", "thead", "tbody", "tr", "td", "th",
]
_STYLED = ["style"]
ALLOWED_ATTRS = {tag: _STYLED for tag in ALLOWED_TAGS}
ALLOWED_ATTRS["td"] = ["style", "colspan", "rowspan"]
ALLOWED_ATTRS["th"] = ["style", "colspan", "rowspan"]
# Только оформление текста — без позиционирования, фонов-картинок и т.п.:
# документ должен остаться документом на бланке.
ALLOWED_CSS = [
    "font-family", "font-size", "font-weight", "font-style", "text-decoration", "text-decoration-line",
    "text-align", "text-indent", "line-height", "margin-left", "padding-left", "vertical-align",
    "border", "border-collapse", "width", "padding",
]
_css_sanitizer = CSSSanitizer(allowed_css_properties=ALLOWED_CSS)

MAX_HTML_LENGTH = 200_000


def sanitize_html(raw: str | None) -> str:
    return bleach.clean(
        raw or "", tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, css_sanitizer=_css_sanitizer, strip=True,
    ).strip()


def has_text(html: str) -> bool:
    text = bleach.clean(html, tags=[], strip=True).replace("&nbsp;", " ").replace("_", " ")
    return bool(text.strip())


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------

def next_number(today: dt.date) -> str:
    """«<год>-<порядковый номер в году>», как у заказных писем
    (postal_letters.next_reg_number) — счётчик не уменьшается."""
    counter = database.db_session.get(ApplicationCounter, today.year)
    if counter is None:
        counter = ApplicationCounter(year=today.year, last_number=0)
        database.db_session.add(counter)
    counter.last_number += 1
    return f"{today.year}-{counter.last_number}"


def applicant_snapshot(person: Person) -> dict:
    """Реквизиты заявителя для шапки — сохраняются в заявлении на момент подачи."""
    passport = None
    if person.passport_series or person.passport_number:
        passport = f"серия {person.passport_series or '—'} № {person.passport_number or '—'}"
        if person.passport_issue_date:
            passport += f", выдан {person.passport_issue_date.strftime('%d.%m.%Y')}"
    phones = ", ".join(p.number for p in person.phones)
    return {
        "applicant_name": person.full_name,
        "applicant_address": person.residence_address or person.registration_address or None,
        "applicant_passport": passport,
        "applicant_phone": phones or None,
    }


def _current_person() -> Person | None:
    return database.db_session.get(Person, g.user.person_id) if g.user.person_id else None


def _own_garages(person: Person | None) -> list[Garage]:
    if person is None:
        return []
    ownerships = database.db_session.query(GarageOwnership).filter_by(person_id=person.id).all()
    return sorted((o.garage for o in ownerships), key=lambda gar: gar.number)


def can_view(application: Application) -> bool:
    return is_board() or (g.user.person_id is not None and g.user.person_id == application.person_id)


def _get_or_404(application_id: int) -> Application:
    application = database.db_session.get(Application, application_id)
    if application is None:
        abort(404)
    if not can_view(application):
        abort(403)
    return application


def visible_applications(person_id: int | None = None, garage_id: int | None = None) -> list[Application]:
    """Заявления для блока на карточке человека/гаража — правлению все,
    рядовому члену только свои (заявление соседа-содольщика по гаражу —
    не его дело)."""
    query = database.db_session.query(Application)
    if person_id is not None:
        query = query.filter(Application.person_id == person_id)
    if garage_id is not None:
        query = query.filter(Application.garage_id == garage_id)
    if not is_board():
        query = query.filter(Application.person_id == g.user.person_id)
    return query.order_by(Application.created_at.desc()).all()


# Шаблонные хелперы — глобалами, а не context_processor: нужны и в
# импортируемых макросах (applications/_macros.html), куда контекст
# страницы без «with context» не попадает.
bp.add_app_template_global(application_heading)
bp.add_app_template_global(status_badge, "application_status_badge")
bp.add_app_template_global(kind_label, "application_kind_label")
bp.add_app_template_global(lambda kind: KIND_HEADINGS[kind], "application_kind_heading")
bp.add_app_template_global(lambda kind: KIND_ACCUSATIVE[kind], "application_kind_accusative")
bp.add_app_template_global(short_name_of, "short_name")


# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def list_applications():
    query = database.db_session.query(Application)
    if not is_board():
        query = query.filter(Application.person_id == g.user.person_id)
    status = request.args.get("status") or ""
    if status in {s.value for s in ApplicationStatus}:
        query = query.filter(Application.status == ApplicationStatus(status))
    applications = query.order_by(Application.created_at.desc()).all()
    read_ids = set()
    if is_board() and g.user.person_id:
        read_ids = {
            app_id for (app_id,) in database.db_session.query(ApplicationRead.application_id)
            .filter(ApplicationRead.person_id == g.user.person_id)
        }
    return render_template(
        "applications/list.html", applications=applications, status=status, read_ids=read_ids,
        can_submit=bool(_own_garages(_current_person())),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    person = _current_person()
    garages = _own_garages(person)
    if not garages:
        flash(_("Писать заявления в правление могут только собственники гаражей — члены кооператива."), "warning")
        return redirect(url_for("applications.list_applications"))

    if request.method == "POST":
        f = request.form
        try:
            kind = ApplicationKind(f.get("kind"))
        except ValueError:
            kind = None
        garage = next((gar for gar in garages if str(gar.id) == f.get("garage_id")), None)
        title = (f.get("title") or "").strip()[:255]
        raw_body = f.get("body_html") or ""
        body_html = sanitize_html(raw_body)

        error = None
        if kind is None:
            error = _("Выберите вид заявления.")
        elif garage is None:
            error = _("Выберите гараж, по которому подаётся заявление.")
        elif len(raw_body) > MAX_HTML_LENGTH:
            error = _("Текст заявления слишком длинный.")
        elif not has_text(body_html):
            error = _("Напишите текст заявления.")
        if error:
            flash(error, "danger")
            return _render_editor(person, garages, kind=kind, garage=garage, title=title, body_html=body_html)

        today = dt.date.today()
        application = Application(
            number=next_number(today), kind=kind, title=title, body_html=body_html,
            person_id=person.id, garage_id=garage.id, garage_number=garage.number,
            created_at=dt.datetime.now(), **applicant_snapshot(person),
        )
        database.db_session.add(application)
        database.db_session.flush()
        audit.record(
            "application.submit", entity_type="application", entity_id=application.id,
            summary=f"{person.full_name} подал(а) {application_heading(application).lower()} "
                    f"№ {application.number} по гаражу № {garage.number}",
        )
        database.db_session.commit()
        flash(_("Заявление № {number} отправлено правлению.", number=application.number), "success")
        return redirect(url_for("applications.detail", application_id=application.id))

    preselect = request.args.get("garage_id", type=int)
    garage = next((gar for gar in garages if gar.id == preselect), garages[0])
    return _render_editor(person, garages, kind=ApplicationKind.OWNERSHIP_CHANGE, garage=garage)


def _render_editor(person, garages, kind, garage, title=None, body_html=None):
    coop, _chairman = _coop_and_chairman()
    templates = {
        k.value: {"heading": KIND_HEADINGS[k], "title": DEFAULT_TITLES[k], "body": DEFAULT_BODIES[k],
                  "label": kind_label(k)}
        for k in ApplicationKind
    }
    kind = kind or ApplicationKind.OWNERSHIP_CHANGE
    garage = garage or garages[0]
    if body_html is None:
        title = DEFAULT_TITLES[kind]
        body_html = DEFAULT_BODIES[kind].format(garage=garage.number)
    return render_template(
        "applications/form.html", person=person, snapshot=applicant_snapshot(person), garages=garages,
        kind=kind, garage=garage, title=title or "", body_html=body_html, templates=templates,
        coop=coop, today=dt.date.today(), kinds=list(ApplicationKind),
    )


@bp.route("/<int:application_id>")
@login_required
def detail(application_id):
    application = _get_or_404(application_id)
    if is_board() and g.user.person_id and g.user.person_id != application.person_id:
        already = any(r.person_id == g.user.person_id for r in application.reads)
        if not already:
            database.db_session.add(ApplicationRead(
                application_id=application.id, person_id=g.user.person_id, read_at=dt.datetime.now(),
            ))
            database.db_session.commit()
            database.db_session.refresh(application)
    coop, _chairman = _coop_and_chairman()
    can_decide = is_board() and application.status == ApplicationStatus.SUBMITTED
    return render_template(
        "applications/detail.html", application=application, coop=coop, can_decide=can_decide,
        today=dt.date.today(),
    )


@bp.route("/<int:application_id>/decide", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def decide(application_id):
    application = _get_or_404(application_id)
    if application.status != ApplicationStatus.SUBMITTED:
        flash(_("Решение по этому заявлению уже принято."), "warning")
        return redirect(url_for("applications.detail", application_id=application.id))

    decision = request.form.get("decision")
    raw = request.form.get("response_html") or ""
    response_html = sanitize_html(raw)
    if decision not in ("answer", "reject"):
        flash(_("Выберите: ответить или отклонить."), "danger")
        return redirect(url_for("applications.detail", application_id=application.id))
    if len(raw) > MAX_HTML_LENGTH:
        flash(_("Текст ответа слишком длинный."), "danger")
        return redirect(url_for("applications.detail", application_id=application.id))
    if decision == "answer" and not has_text(response_html):
        flash(_("Напишите текст ответа."), "danger")
        return redirect(url_for("applications.detail", application_id=application.id))

    application.status = ApplicationStatus.ANSWERED if decision == "answer" else ApplicationStatus.REJECTED
    application.response_html = response_html if has_text(response_html) else None
    application.decided_at = dt.datetime.now()
    application.decided_by_person_id = g.user.person_id

    heading = application_heading(application)
    verdict = "дан ответ" if decision == "answer" else "отклонено"
    audit.record(
        f"application.{decision}", entity_type="application", entity_id=application.id,
        summary=f"{heading} № {application.number} ({application.applicant_name}): {verdict}",
    )
    applicant = notifications.user_for_person(application.person_id)
    if applicant is not None:
        link = request.host_url.rstrip("/") + url_for("applications.detail", application_id=application.id)
        if decision == "answer":
            subject = f"Ответ правления на заявление № {application.number}"
            body = f"Правление ответило на ваше заявление № {application.number} «{heading}».\n\n{link}"
        else:
            subject = f"Заявление № {application.number} отклонено"
            body = f"Правление отклонило ваше заявление № {application.number} «{heading}».\n\n{link}"
        notifications.after_commit(notifications.notify_user_id, applicant.id, "my_requests", subject, body)
    database.db_session.commit()
    flash(_("Ответ отправлен заявителю.") if decision == "answer" else _("Заявление отклонено."), "success")
    return redirect(url_for("applications.detail", application_id=application.id))


@bp.route("/<int:application_id>/pdf")
@login_required
def pdf(application_id):
    application = _get_or_404(application_id)
    coop, _chairman = _coop_and_chairman()
    return _render_pdf_or_fallback(
        "applications/pdf.html", f"application-{application.number}.pdf", "applications/detail.html",
        application=application, coop=coop, can_decide=False, today=dt.date.today(),
    )
