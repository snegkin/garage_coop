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
- Заявление о смене собственника: заявитель прямо на бланке заполняет
  сведения о новом собственнике (NEW_OWNER_FIELDS) и прикладывает сканы
  документов. Член правления проверяет/поправляет данные и проводит смену
  (transfer): новая карточка человека или уже существующий человек, доля
  заявителя целиком переходит к нему (garages.transfer_owner), приложения
  копируются в документы гаража.
"""
import datetime as dt
import json
import os
import shutil
import uuid

import bleach
from bleach.css_sanitizer import CSSSanitizer
from markupsafe import escape
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, current_app, send_file

from . import audit
from . import database
from . import notifications
from .i18n import translate as _
from .auth import login_required, roles_required
from .legal_docs import _render_pdf_or_fallback, _coop_and_chairman
from .permissions import is_board
from .uploads import save_upload
from .models import (
    Application, ApplicationAttachment, ApplicationRead, ApplicationCounter, ApplicationKind, ApplicationStatus,
    Garage, GarageDocumentPhoto, GarageDocumentType, GarageOwnership, Person, Phone, RoleEnum, short_name_of,
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
        "<p>Прошу в связи с переходом права собственности на гараж № {garage} исключить меня из "
        "собственников гаража и внести в реестр нового собственника, сведения о котором приведены ниже.</p>"
        "<p>Копии документов, подтверждающих переход права собственности, прилагаю.</p>"
    ),
    ApplicationKind.CLAIM: (
        "<p>Настоящим заявляю претензию по следующему вопросу: ____________________.</p>"
        "<p>Обстоятельства: ____________________.</p>"
        "<p>Требую: ____________________.</p>"
        "<p>Прошу рассмотреть претензию и дать письменный ответ в течение 30 дней.</p>"
    ),
    ApplicationKind.OTHER: "<p>Прошу ____________________.</p>",
}


# Сведения о новом собственнике (заявление о смене собственника) — поля
# таблицы на бланке; ключи совпадают с полями Person, кроме phone (→ Phone)
# и basis (основание перехода права — только в тексте заявления/журнале).
NEW_OWNER_FIELDS = [
    ("full_name", "ФИО"),
    ("birth_date", "Дата рождения"),
    ("passport_series", "Паспорт: серия"),
    ("passport_number", "Паспорт: номер"),
    ("passport_issue_date", "Паспорт: дата выдачи"),
    ("registration_address", "Адрес регистрации"),
    ("residence_address", "Адрес проживания"),
    ("phone", "Телефон"),
    ("email", "Email"),
    ("basis", "Основание перехода права"),
]
NEW_OWNER_DATE_FIELDS = {"birth_date", "passport_issue_date"}
NEW_OWNER_PERSON_FIELDS = (
    "birth_date", "passport_series", "passport_number", "passport_issue_date",
    "registration_address", "residence_address", "email",
)


def parse_new_owner(form, prefix: str = "no_") -> tuple[dict, str | None]:
    """Сведения о новом собственнике из формы (поля <prefix><ключ>) →
    (словарь непустых значений, ошибка или None). Даты — ГГГГ-ММ-ДД
    (input type=date), в словаре хранятся так же."""
    data = {}
    for key, _label in NEW_OWNER_FIELDS:
        value = (form.get(prefix + key) or "").strip()[:255]
        if value:
            data[key] = value
    if not data.get("full_name"):
        return data, _("Укажите ФИО нового собственника.")
    for key in NEW_OWNER_DATE_FIELDS:
        if key in data:
            try:
                dt.date.fromisoformat(data[key])
            except ValueError:
                return data, _("Неверная дата в сведениях о новом собственнике.")
    if "passport_series" in data and not (data["passport_series"].isdigit() and len(data["passport_series"]) == 4):
        return data, _("Серия паспорта — 4 цифры.")
    if "passport_number" in data and not (data["passport_number"].isdigit() and len(data["passport_number"]) == 6):
        return data, _("Номер паспорта — 6 цифр.")
    return data, None


def new_owner_rows(data: dict) -> list[tuple[str, str]]:
    """(подпись, значение) для таблицы на бланке — даты в ДД.ММ.ГГГГ."""
    rows = []
    for key, label in NEW_OWNER_FIELDS:
        value = data.get(key)
        if value and key in NEW_OWNER_DATE_FIELDS:
            value = dt.date.fromisoformat(value).strftime("%d.%m.%Y")
        rows.append((label, value or ""))
    return rows


def matching_persons(full_name: str) -> list[Person]:
    """Люди из реестра с тем же ФИО (без учёта регистра и «ё») — кандидаты,
    что новый собственник уже есть в базе. Сравнение в Python: SQLite
    lower()/ILIKE не понимают кириллицу."""
    key = " ".join(full_name.lower().replace("ё", "е").split())
    return [
        p for p in database.db_session.query(Person).order_by(Person.full_name).all()
        if " ".join(p.full_name.lower().replace("ё", "е").split()) == key
    ]


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
MAX_ATTACHMENTS = 10


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
bp.add_app_template_global(new_owner_rows, "application_new_owner_rows")


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
        new_owner = {}
        if error is None and kind == ApplicationKind.OWNERSHIP_CHANGE:
            new_owner, error = parse_new_owner(f)
        stored = []
        if error is None:
            stored, error = _save_uploads("attachments")
        if error:
            flash(error, "danger")
            return _render_editor(person, garages, kind=kind, garage=garage, title=title, body_html=body_html,
                                  new_owner=parse_new_owner(f)[0])

        today = dt.date.today()
        application = Application(
            number=next_number(today), kind=kind, title=title, body_html=body_html,
            person_id=person.id, garage_id=garage.id, garage_number=garage.number,
            created_at=dt.datetime.now(), **applicant_snapshot(person),
            new_owner_data=json.dumps(new_owner, ensure_ascii=False) if new_owner else None,
        )
        for original, name in stored:
            application.attachments.append(ApplicationAttachment(original_filename=original, file_path=name))
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


def _save_uploads(field: str) -> tuple[list[tuple[str, str]], str | None]:
    """Файлы из поля формы → [(исходное имя, имя на диске)], ошибка. При
    ошибке уже сохранённые файлы этого вызова удаляются."""
    uploads = [u for u in request.files.getlist(field) if u and u.filename]
    if len(uploads) > MAX_ATTACHMENTS:
        return [], _("Не больше {n} файлов-приложений.", n=MAX_ATTACHMENTS)
    stored = []
    for upload in uploads:
        name = save_upload(upload, current_app.config["UPLOAD_FOLDER"])
        if name is None:
            _remove_files(n for _orig, n in stored)
            return [], _("Недопустимый тип файла: {name}", name=upload.filename)
        stored.append((upload.filename[:255], name))
    return stored, None


def _remove_files(names) -> None:
    for name in names:
        path = os.path.join(current_app.config["UPLOAD_FOLDER"], name)
        if os.path.exists(path):
            os.remove(path)


@bp.route("/register", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def register_paper():
    """Завести заявление, поданное на бумаге: член правления выбирает
    заявителя-собственника и гараж, вид, дату на бланке и загружает скан
    подписанного бланка — текст не набирается. Дальше — как с любым
    заявлением (ознакомление, ответ/отклонение, смена собственника)."""
    ownerships = sorted(
        database.db_session.query(GarageOwnership).all(),
        key=lambda o: (o.person.full_name.lower(), o.garage.number),
    )
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        ownership = next((o for o in ownerships if f"{o.person_id}:{o.garage_id}" == form.get("owner")), None)
        try:
            kind = ApplicationKind(form.get("kind"))
        except ValueError:
            kind = None
        try:
            paper_date = dt.date.fromisoformat(form.get("paper_date") or "")
        except ValueError:
            paper_date = None
        title = (form.get("title") or "").strip()[:255]

        error = None
        if ownership is None:
            error = _("Выберите заявителя и гараж.")
        elif kind is None:
            error = _("Выберите вид заявления.")
        elif paper_date is None or paper_date > dt.date.today():
            error = _("Укажите дату заявления (не позже сегодняшней).")
        scans, extras = [], []
        if error is None:
            scans, error = _save_uploads("scan")
            if error is None and not scans:
                error = _("Загрузите скан подписанного заявления.")
        if error is None:
            extras, error = _save_uploads("attachments")
        if error:
            _remove_files(n for _o, n in scans)
            flash(error, "danger")
            return _render_register(ownerships, form)

        person, garage = ownership.person, ownership.garage
        application = Application(
            number=next_number(paper_date), kind=kind, title=title, body_html="",
            person_id=person.id, garage_id=garage.id, garage_number=garage.number,
            created_at=dt.datetime.combine(paper_date, dt.datetime.now().time()),
            registered_by_user_id=g.user.id, **applicant_snapshot(person),
        )
        for original, name in scans:
            application.attachments.append(ApplicationAttachment(original_filename=original, file_path=name, is_scan=True))
        for original, name in extras:
            application.attachments.append(ApplicationAttachment(original_filename=original, file_path=name))
        database.db_session.add(application)
        database.db_session.flush()
        if g.user.person_id:
            database.db_session.add(ApplicationRead(
                application_id=application.id, person_id=g.user.person_id, read_at=dt.datetime.now(),
            ))
        audit.record(
            "application.register", entity_type="application", entity_id=application.id,
            summary=f"Зарегистрировано бумажное {application_heading(application).lower()} "
                    f"№ {application.number} от {person.full_name} по гаражу № {garage.number}",
        )
        database.db_session.commit()
        flash(_("Заявление № {number} зарегистрировано.", number=application.number), "success")
        return redirect(url_for("applications.detail", application_id=application.id))

    return _render_register(ownerships, form)


def _render_register(ownerships, form):
    selected = form.get("owner")
    if not selected:
        person_id = form.get("person_id", type=int)
        garage_id = form.get("garage_id", type=int)
        match = next((o for o in ownerships
                      if (person_id is None or o.person_id == person_id) and (garage_id is None or o.garage_id == garage_id)
                      and (person_id or garage_id)), None)
        selected = f"{match.person_id}:{match.garage_id}" if match else ""
    return render_template(
        "applications/register.html", ownerships=ownerships, selected=selected,
        kinds=list(ApplicationKind), kind=form.get("kind") or ApplicationKind.OWNERSHIP_CHANGE.value,
        title=form.get("title") or "", paper_date=form.get("paper_date") or dt.date.today().isoformat(),
        default_titles={k.value: DEFAULT_TITLES[k] for k in ApplicationKind}, today_iso=dt.date.today().isoformat(),
    )


def _render_editor(person, garages, kind, garage, title=None, body_html=None, new_owner=None):
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
        new_owner=new_owner or {}, new_owner_fields=NEW_OWNER_FIELDS, date_fields=NEW_OWNER_DATE_FIELDS,
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
        today=dt.date.today(), transfer=_transfer_context(application) if can_decide else None,
        response_prefill=_response_prefill(application),
    )


def _applicant_ownership(application: Application) -> GarageOwnership | None:
    if application.garage_id is None:
        return None
    return (
        database.db_session.query(GarageOwnership)
        .filter_by(garage_id=application.garage_id, person_id=application.person_id)
        .first()
    )


def _transfer_context(application: Application) -> dict | None:
    """Данные для формы «Провести смену собственника» на карточке
    заявления — только для заявления о смене собственника, ещё не
    проведённого."""
    if application.kind != ApplicationKind.OWNERSHIP_CHANGE or application.transferred_at:
        return None
    data = application.new_owner_dict
    ownership = _applicant_ownership(application)
    return {
        "data": data, "ownership": ownership,
        "matches": matching_persons(data["full_name"]) if data.get("full_name") else [],
        "persons": database.db_session.query(Person).filter(Person.is_archived.is_(False))
                   .order_by(Person.full_name).all(),
        "fields": NEW_OWNER_FIELDS, "date_fields": NEW_OWNER_DATE_FIELDS,
        "comment": f"По заявлению № {application.number} от {application.created_at.strftime('%d.%m.%Y')}"
                   + (f"; основание: {data['basis']}" if data.get("basis") else ""),
    }


def _response_prefill(application: Application) -> str:
    if application.transferred_at and application.new_owner:
        return (
            f"<p>Правление рассмотрело Ваше заявление. Смена собственника гаража № {escape(application.garage_number)} "
            f"произведена {application.transferred_at.strftime('%d.%m.%Y')}: собственником гаража внесён(а) "
            f"{escape(application.new_owner.full_name)}.</p>"
        )
    return "<p>Правление рассмотрело Ваше заявление.</p><p><br></p>"


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


@bp.route("/<int:application_id>/transfer", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def transfer(application_id):
    """Провести смену собственника по заявлению: человек — новая карточка
    по (поправленным правлением) сведениям из заявления или уже
    существующий в реестре (пустые поля его карточки дополняются из
    заявления, заполненные не трогаются); доля заявителя целиком переходит
    к нему (garages.transfer_owner); приложения заявления копируются в
    документы гаража."""
    from .garages import transfer_owner

    application = _get_or_404(application_id)
    back = redirect(url_for("applications.detail", application_id=application.id))
    if application.kind != ApplicationKind.OWNERSHIP_CHANGE or application.status != ApplicationStatus.SUBMITTED:
        flash(_("Смену собственника можно провести только по заявлению о смене собственника, ожидающему решения."), "danger")
        return back
    if application.transferred_at:
        flash(_("Смена собственника по этому заявлению уже проведена."), "warning")
        return back
    ownership = _applicant_ownership(application)
    if ownership is None:
        flash(_("Заявитель уже не числится собственником этого гаража — смену нужно провести вручную на карточке гаража."), "danger")
        return back

    f = request.form
    data, error = parse_new_owner(f)
    if error:
        flash(error, "danger")
        return back
    target = f.get("target") or "new"
    if target == "new":
        new_person = Person(full_name=data["full_name"])
        database.db_session.add(new_person)
    else:
        new_person = database.db_session.get(Person, int(target)) if target.isdigit() else None
        if new_person is None:
            flash(_("Выберите человека из реестра."), "danger")
            return back
    if new_person.id is not None and new_person.id == application.person_id:
        flash(_("Новый собственник совпадает с заявителем."), "danger")
        return back
    for key in NEW_OWNER_PERSON_FIELDS:
        if data.get(key) and not getattr(new_person, key):
            value = dt.date.fromisoformat(data[key]) if key in NEW_OWNER_DATE_FIELDS else data[key]
            setattr(new_person, key, value)
    database.db_session.flush()
    phone = data.get("phone")
    if phone and phone not in {p.number for p in new_person.phones}:
        database.db_session.add(Phone(person_id=new_person.id, number=phone[:30]))

    garage = application.garage
    comment = (f.get("comment") or "").strip()[:500] or None
    transfer_owner(garage, ownership, new_person.id, carry_balance=bool(f.get("carry_balance")),
                   comment=comment, user_id=g.user.id)

    for att in application.attachments:
        src = os.path.join(current_app.config["UPLOAD_FOLDER"], att.file_path)
        if not os.path.exists(src):
            continue
        copy_name = f"{uuid.uuid4().hex}{os.path.splitext(att.file_path)[1]}"
        shutil.copyfile(src, os.path.join(current_app.config["UPLOAD_FOLDER"], copy_name))
        database.db_session.add(GarageDocumentPhoto(
            garage_id=garage.id, doc_type=GarageDocumentType.OTHER, original_filename=att.original_filename,
            file_path=copy_name, comment=f"К заявлению № {application.number}", author_id=g.user.id,
        ))

    application.new_owner_person_id = new_person.id
    application.new_owner_data = json.dumps(data, ensure_ascii=False)
    application.transferred_at = dt.datetime.now()
    audit.record(
        "application.transfer", entity_type="application", entity_id=application.id,
        summary=f"По заявлению № {application.number} проведена смена собственника гаража № {garage.number}: "
                f"{application.applicant_name} → {new_person.full_name}",
    )
    database.db_session.commit()
    flash(_("Смена собственника проведена. Осталось отправить заявителю ответ."), "success")
    return back


@bp.route("/attachments/<int:attachment_id>")
@login_required
def attachment_file(attachment_id):
    """?inline=1 — показать в браузере (скан на карточке заявления:
    картинка/PDF во встроенном просмотре), иначе — скачать."""
    att = database.db_session.get(ApplicationAttachment, attachment_id)
    if att is None:
        abort(404)
    _get_or_404(att.application_id)
    inline = bool(request.args.get("inline")) and (att.is_image or att.is_pdf)
    return send_file(
        os.path.join(current_app.config["UPLOAD_FOLDER"], att.file_path),
        as_attachment=not inline, download_name=att.original_filename,
    )


@bp.route("/<int:application_id>/pdf")
@login_required
def pdf(application_id):
    application = _get_or_404(application_id)
    if application.is_paper and application.status == ApplicationStatus.SUBMITTED:
        # Сам документ — скан, печатной формы нет; в PDF пошёл бы только ответ.
        return redirect(url_for("applications.detail", application_id=application.id))
    coop, _chairman = _coop_and_chairman()
    return _render_pdf_or_fallback(
        "applications/pdf.html", f"application-{application.number}.pdf", "applications/detail.html",
        application=application, coop=coop, can_decide=False, today=dt.date.today(),
    )
