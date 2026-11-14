"""
Бланки и шаблоны документов (`/documents/`) — библиотека шаблонов на
официальном бланке кооператива (шапка с логотипом и реквизитами —
legal_docs/_letterhead.html): объявления (в т. ч. об общем собрании),
справки, уведомления, информационные листы (сайт, группы в соцсетях,
реквизиты).

Шаблон (DocumentTemplate) — заголовок, текст правой колонки шапки
(«Кому», «по месту требования») и текст документа, набранные в том же
редакторе на бланке, что у заявлений (applications/_editor.html), с
подстановками {{ИНН}}, {{ФИО}}, {{задолженность}} … (VARIABLES). Кому
документ — никому (объявление), человеку или собственнику гаража:
от этого зависит, какие подстановки доступны.

Документ (IssuedDocument) составляется по шаблону: правление выбирает
адресата, система подставляет данные реестра, текст правится прямо на
бланке и сохраняется в журнал (с номером «<год>-<N>», если у шаблона
включена нумерация) — повторная печать того же текста. Ничего не
отправляется само — только печать и PDF.

Справка о владении гаражом (для «гаражной амнистии») — обычный шаблон
библиотеки с меткой code="garage_certificate" и «только председатель»;
то, что раньше собиралось кодом, — подстановки {{он_она}}, {{членство}},
{{владение}} (целиком / доля в праве), {{сведения_о_гараже}},
{{нет_задолженности}} (фраза — только если долга действительно нет).
Запросы справок из личного кабинета (app/certificates.py,
CertificateRequest) оформляются здесь же: create?request_id=…, документ
привязывается к запросу. Документы, адресованные человеку, он видит сам
(кабинет, карточки), остальное — только правление.

Уведомления о задолженности с выпиской по годам и иски — в «Должниках»
(app/legal_docs.py), здесь — простое уведомление-письмо.
"""
import datetime as dt
import re
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from markupsafe import Markup, escape

from . import audit
from . import database
from . import name_declension
from .accounting import balance, get_primary_bank_account
from .applications import sanitize_html, has_text, MAX_HTML_LENGTH
from .auth import login_required, roles_required
from .i18n import translate as _
from .legal_docs import _render_pdf_or_fallback, _coop_and_chairman
from .models import short_name_of
from .models import (
    CertificateRequest, CertificateRequestStatus, DocumentCounter, DocumentTemplate, Garage,
    GarageOwnership, IssuedDocument, MemberAccount, Person, RoleEnum,
)
from .permissions import is_board, is_chairman

bp = Blueprint("documents", __name__, url_prefix="/documents")

RECIPIENTS = {
    "none": "без адресата (объявление, информационный лист)",
    "person": "человек из реестра",
    "garage": "собственник гаража",
}

BLANK = "________"
CERTIFICATE_CODE = "garage_certificate"


# ---------------------------------------------------------------------------
# Подстановки
# ---------------------------------------------------------------------------

def _date(value: dt.date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _passport(person: Person) -> str:
    if not (person.passport_series or person.passport_number):
        return ""
    text = f"серия {person.passport_series or '—'} № {person.passport_number or '—'}"
    if person.passport_issue_date:
        text += f", выдан {_date(person.passport_issue_date)}"
    return text


def _share(share) -> str:
    from fractions import Fraction
    if share is None or share == 1:
        return "1/1"
    fraction = Fraction(share).limit_denominator(100)
    return f"{fraction.numerator}/{fraction.denominator}"


def person_debt(person: Person) -> Decimal:
    """Долг человека перед кооперативом: отрицательные балансы его
    действующих лицевых счетов + электричество по его гаражам (как
    certificates.debt_total, но по всем гаражам сразу)."""
    total = Decimal("0")
    for account in database.db_session.query(MemberAccount).filter_by(person_id=person.id, is_archived=False):
        b = balance(account)
        if b < 0:
            total -= b
    garages = {o.garage_id: o.garage for o in person_ownerships(person)}
    for garage in garages.values():
        if garage.account is not None:
            b = balance(garage)
            if b < 0:
                total -= b
    return total


def person_ownerships(person: Person) -> list[GarageOwnership]:
    return sorted(
        database.db_session.query(GarageOwnership).filter_by(person_id=person.id).all(),
        key=lambda o: o.garage.number,
    )


def _coop_short(ctx) -> str:
    # Краткое название («ГК «Восход»») не склоняется; полное в «членом …»
    # пришлось бы склонять — без краткого пишем просто «кооператива».
    return ctx.coop.short_name if ctx.coop and ctx.coop.short_name else "кооператива"


def _pronoun(ctx) -> str:
    if ctx.person is None:
        return ""
    return {"male": "он", "female": "она"}.get(name_declension.gender(ctx.person.full_name), "он(а)")


def _membership(ctx) -> str:
    p = ctx.person
    if p is None or not p.membership_start_date or p.membership_end_date:
        return ""
    return f"является членом {_coop_short(ctx)} с {_date(p.membership_start_date)} и "


def _ownership_phrase(ctx) -> str:
    if ctx.garage is None or ctx.ownership is None:
        return ""
    if ctx.ownership.share == 1:
        return f"гаражом № {ctx.garage.number}, расположенным"
    return f"{_share(ctx.ownership.share)} доли в праве на гараж № {ctx.garage.number}, расположенный"


def _garage_facts(ctx) -> str:
    gr = ctx.garage
    if gr is None:
        return ""
    facts = [f"площадью {str(gr.area_sqm).replace('.', ',')} м²"]
    if gr.cadastral_number:
        facts.append(f"кадастровый номер гаража {gr.cadastral_number}")
    if gr.land_cadastral_number:
        facts.append(f"кадастровый номер земельного участка {gr.land_cadastral_number}")
    return ", ".join(facts)


def _no_debt(ctx) -> str:
    if ctx.person is None or person_debt(ctx.person) != 0:
        return ""
    return "Задолженности по взносам и платежам перед кооперативом не имеет."


class Context:
    """Данные для подстановок: кооператив, председатель, основной
    расчётный счёт, дата документа и (если есть) адресат."""

    def __init__(self, today: dt.date, person: Person | None = None, garage: Garage | None = None,
                 public: bool = False):
        self.coop, self.chairman = _coop_and_chairman()
        # Общедоступный бланк — без банковских реквизитов (как и публичная
        # страница кооператива): в бланке прочерк.
        self.bank = None if public else get_primary_bank_account()
        self.today = today
        self.person = person
        self.garage = garage
        self.ownership = None
        if person is not None and garage is not None:
            self.ownership = next((o for o in garage.ownerships if o.person_id == person.id), None)

    def coop_attr(self, name: str) -> str:
        return (getattr(self.coop, name, None) or "") if self.coop else ""

    def bank_attr(self, name: str) -> str:
        return (getattr(self.bank, name, None) or "") if self.bank else ""

    def person_attr(self, fn) -> str:
        return fn(self.person) if self.person is not None else ""

    def garages_text(self) -> str:
        if self.garage is not None:
            return self.garage.number
        if self.person is not None:
            return ", ".join(dict.fromkeys(o.garage.number for o in person_ownerships(self.person)))
        return ""

    def garage_attr(self, fn) -> str:
        return fn(self.garage) if self.garage is not None else ""


# (ключ, описание, группа, функция(ctx) -> str). Значения — обычный текст
# (экранируется при подстановке), переносы строк — в <br>.
VARIABLES = [
    ("кооператив", "полное наименование кооператива", "Кооператив", lambda c: c.coop_attr("full_name")),
    ("кооператив_кратко", "краткое наименование (или полное, если краткого нет)", "Кооператив",
     lambda c: c.coop_attr("short_name") or c.coop_attr("full_name")),
    ("ИНН", "ИНН кооператива", "Кооператив", lambda c: c.coop_attr("inn")),
    ("КПП", "КПП кооператива", "Кооператив", lambda c: c.coop_attr("kpp")),
    ("ОГРН", "ОГРН кооператива", "Кооператив", lambda c: c.coop_attr("ogrn")),
    ("дата_регистрации", "дата регистрации кооператива", "Кооператив",
     lambda c: _date(c.coop.registration_date) if c.coop else ""),
    ("юр_адрес", "юридический адрес", "Кооператив", lambda c: c.coop_attr("legal_address")),
    ("почтовый_адрес", "почтовый адрес (или юридический)", "Кооператив",
     lambda c: c.coop_attr("postal_address") or c.coop_attr("legal_address")),
    ("телефон_кооператива", "телефон кооператива", "Кооператив", lambda c: c.coop_attr("phone")),
    ("email", "электронная почта кооператива", "Кооператив", lambda c: c.coop_attr("email")),
    ("сайт", "сайт кооператива", "Кооператив", lambda c: c.coop_attr("website")),
    ("соцсети", "группы в соцсетях и мессенджерах (по строке на группу)", "Кооператив",
     lambda c: c.coop_attr("social_links")),
    ("банк", "банк основного расчётного счёта", "Кооператив", lambda c: c.bank_attr("bank_name")),
    ("БИК", "БИК банка", "Кооператив", lambda c: c.bank_attr("bik")),
    ("расчётный_счёт", "расчётный счёт", "Кооператив", lambda c: c.bank_attr("checking_account")),
    ("корр_счёт", "корреспондентский счёт", "Кооператив", lambda c: c.bank_attr("correspondent_account")),
    ("председатель", "ФИО председателя полностью", "Кооператив",
     lambda c: c.chairman.full_name if c.chairman else ""),
    ("председатель_кратко", "председатель: «Иванов И.И.»", "Кооператив",
     lambda c: c.chairman.short_name if c.chairman else ""),
    ("председатель_кому", "председатель в дательном падеже: «Иванову Ивану Ивановичу»", "Кооператив",
     lambda c: name_declension.dative(c.chairman.full_name) if c.chairman else ""),
    ("председатель_кратко_кому", "председатель в дательном падеже: «Иванову И.И.» (заявления)", "Кооператив",
     lambda c: short_name_of(name_declension.dative(c.chairman.full_name)) if c.chairman else ""),
    ("сегодня", "дата документа", "Дата", lambda c: _date(c.today)),
    ("год", "год даты документа", "Дата", lambda c: str(c.today.year)),
    ("сегодня+10", "дата документа плюс N дней — срок (любое N: {{сегодня+30}}, {{сегодня-1}})", "Дата",
     lambda c: _date(c.today + dt.timedelta(days=10))),
    ("ФИО", "ФИО адресата", "Адресат", lambda c: c.person_attr(lambda p: p.full_name)),
    ("ФИО_кому", "ФИО в дательном падеже («Петровой Анне Ивановне»)", "Адресат",
     lambda c: c.person_attr(lambda p: name_declension.dative(p.full_name))),
    ("уважаемый", "«Уважаемый» / «Уважаемая» — по отчеству адресата", "Адресат",
     lambda c: c.person_attr(lambda p: name_declension.respected(p.full_name))),
    ("имя_отчество", "имя и отчество адресата — для обращения («Анна Ивановна»)", "Адресат",
     lambda c: c.person_attr(lambda p: name_declension.first_and_middle(p.full_name))),
    ("ФИО_от_кого", "ФИО в родительном падеже («от Петровой Анны Ивановны»)", "Адресат",
     lambda c: c.person_attr(lambda p: name_declension.genitive(p.full_name))),
    ("ФИО_кратко", "«Петрова А.И.»", "Адресат", lambda c: c.person_attr(lambda p: p.short_name)),
    ("дата_рождения", "дата рождения", "Адресат", lambda c: c.person_attr(lambda p: _date(p.birth_date))),
    ("паспорт", "паспорт: серия, номер, дата выдачи", "Адресат", lambda c: c.person_attr(_passport)),
    ("адрес", "адрес проживания (или регистрации)", "Адресат",
     lambda c: c.person_attr(lambda p: p.residence_address or p.registration_address or "")),
    ("адрес_регистрации", "адрес регистрации", "Адресат",
     lambda c: c.person_attr(lambda p: p.registration_address or "")),
    ("телефон", "телефоны адресата", "Адресат",
     lambda c: c.person_attr(lambda p: ", ".join(ph.number for ph in p.phones))),
    ("членство_с", "дата вступления в кооператив", "Адресат",
     lambda c: c.person_attr(lambda p: _date(p.membership_start_date))),
    ("лицевые_счета", "номера действующих лицевых счетов адресата", "Адресат",
     lambda c: c.person_attr(lambda p: ", ".join(
         a.account_number for a in database.db_session.query(MemberAccount)
         .filter_by(person_id=p.id, is_archived=False).order_by(MemberAccount.account_number)
     ))),
    ("задолженность", "долг перед кооперативом на сегодня, ₽ (взносы + электричество)", "Адресат",
     lambda c: c.person_attr(lambda p: _money(person_debt(p)))),
    ("гараж", "номер гаража (у человека — все его гаражи через запятую)", "Гараж", lambda c: c.garages_text()),
    ("площадь_гаража", "площадь гаража, м²", "Гараж",
     lambda c: c.garage_attr(lambda gr: str(gr.area_sqm).replace(".", ","))),
    ("кадастровый_номер", "кадастровый номер гаража", "Гараж",
     lambda c: c.garage_attr(lambda gr: gr.cadastral_number or "")),
    ("кадастровый_номер_участка", "кадастровый номер земельного участка", "Гараж",
     lambda c: c.garage_attr(lambda gr: gr.land_cadastral_number or "")),
    ("доля", "доля адресата в праве на гараж («1/2»)", "Гараж",
     lambda c: _share(c.ownership.share) if c.ownership is not None else ""),
    ("он_она", "«он» / «она» по отчеству адресата", "Для справки о владении", _pronoun),
    ("членство", "«является членом … с <дата> и » — только у действующего члена, иначе пусто",
     "Для справки о владении", _membership),
    ("владение", "«гаражом № 42, расположенным» или «1/2 доли в праве на гараж № 42, расположенный»",
     "Для справки о владении", _ownership_phrase),
    ("сведения_о_гараже", "площадь и кадастровые номера гаража и участка (какие известны)",
     "Для справки о владении", _garage_facts),
    ("нет_задолженности", "фраза «Задолженности … не имеет.» — только если долга нет, иначе пусто",
     "Для справки о владении", _no_debt),
]
# Пустое значение у них — норма (фраза просто не нужна), не прочерк.
OPTIONAL_KEYS = {"членство", "нет_задолженности"}
_BY_KEY = {key.lower(): (key, fn) for key, _desc, _group, fn in VARIABLES}
_EMPTY_P_RE = re.compile(r"<p(?:\s[^>]*)?>(?:\s|&nbsp;)*</p>")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}<>]+?)\s*\}\}")


# {{сегодня+10}}, {{сегодня - 3}} — дата документа плюс/минус N дней
# (срок оплаты, явки и т.п.).
DATE_SHIFT_RE = re.compile(r"^сегодня\s*([+-])\s*(\d{1,4})$", re.IGNORECASE)


def _resolve(raw_key: str, ctx: "Context") -> tuple[str, str] | None:
    """(ключ, значение) подстановки или None, если ключ неизвестен."""
    raw_key = raw_key.strip()
    shift = DATE_SHIFT_RE.match(raw_key)
    if shift:
        days = int(shift.group(2)) * (1 if shift.group(1) == "+" else -1)
        return raw_key, _date(ctx.today + dt.timedelta(days=days))
    found = _BY_KEY.get(raw_key.lower())
    if found is None:
        return None
    key, fn = found
    return key, fn(ctx) or ""


def _is_known(raw_key: str) -> bool:
    raw_key = raw_key.strip()
    return bool(DATE_SHIFT_RE.match(raw_key)) or raw_key.lower() in _BY_KEY


def fill(html: str, ctx: Context, missing: set[str] | None = None) -> str:
    """Подставить значения в HTML шаблона. Неизвестные ключи остаются как
    есть (видно, что опечатка), пустые значения — прочерк для заполнения
    от руки; их ключи складываются в missing."""
    def replace(match: re.Match) -> str:
        resolved = _resolve(match.group(1), ctx)
        if resolved is None:
            return match.group(0)
        key, value = resolved
        if not value.strip():
            if key in OPTIONAL_KEYS:
                return ""
            if missing is not None:
                missing.add(key)
            return BLANK
        return str(Markup("<br>").join(escape(line) for line in value.strip("\n").splitlines()))
    # Абзац, от которого после подстановки ничего не осталось (например,
    # {{нет_задолженности}} при долге), — убирается.
    return _EMPTY_P_RE.sub("", PLACEHOLDER_RE.sub(replace, html or ""))


def fill_text(text: str, ctx: Context, missing: set[str] | None = None) -> str:
    """То же для обычного текста (заголовок документа): без HTML —
    экранирует уже шаблон при выводе; многострочное значение — в строку."""
    def replace(match: re.Match) -> str:
        resolved = _resolve(match.group(1), ctx)
        if resolved is None:
            return match.group(0)
        key, value = resolved
        value = " ".join(value.split())
        if not value:
            if key in OPTIONAL_KEYS:
                return ""
            if missing is not None:
                missing.add(key)
            return BLANK
        return value
    return " ".join(PLACEHOLDER_RE.sub(replace, text or "").split())


def unknown_placeholders(*htmls: str) -> list[str]:
    keys = []
    for html in htmls:
        for match in PLACEHOLDER_RE.finditer(html or ""):
            key = match.group(1).strip()
            if not _is_known(key) and key not in keys:
                keys.append(key)
    return keys


def variable_groups(recipient: str = "garage") -> list[tuple[str, list[tuple[str, str]]]]:
    """Подстановки для панели редактора шаблона — без «Адресата»/«Гаража»,
    если у шаблона их нет."""
    skip = {"none": {"Адресат", "Гараж", "Для справки о владении"}}.get(recipient, set())
    groups: dict[str, list] = {}
    for key, desc, group, _fn in VARIABLES:
        if group not in skip:
            groups.setdefault(group, []).append((key, desc))
    return list(groups.items())


# ---------------------------------------------------------------------------
# Стандартные шаблоны
# ---------------------------------------------------------------------------

# Заявления от члена кооператива: шапка — «В правление …» (не на имя
# председателя: состав правления меняется, бланк — нет) и от кого, внизу — подпись
# заявителя (подпись председателя и М.П. у таких шаблонов выключены).
_TO_BOARD = "<p>В правление {{кооператив_кратко}}</p>"
_FROM_PERSON = "<p>от {{ФИО_от_кого}}</p><p>адрес: {{адрес}}</p><p>телефон: {{телефон}}</p>"
# Отступ перед подписью — пустым абзацем: margin санитайзер не пропускает
# (applications.ALLOWED_CSS). Абзацы с прочерками для заполнения от руки —
# по левому краю (_FILL): по ширине прочерк, ушедший на новую строку,
# растягивает пробелы в предыдущей.
_FILL = "<p style=\"text-align: left\">"
_APPLICANT_SIGN = (
    "<p><br></p>"
    "<p style=\"text-indent: 0; text-align: right\">"
    "«___» ______________ 20___ г.&nbsp;&nbsp;&nbsp;&nbsp;______________ /{{ФИО_кратко}}/</p>"
)

_PAY_DETAILS = (
    "<p>Реквизиты для оплаты: получатель {{кооператив}}, ИНН {{ИНН}}, КПП {{КПП}}, "
    "р/с {{расчётный_счёт}} в {{банк}}, БИК {{БИК}}, к/с {{корр_счёт}}. "
    "В назначении платежа укажите номер лицевого счёта ({{лицевые_счета}}) и номер гаража.</p>"
)

DEFAULT_TEMPLATES = [
    {
        "name": "Справка о владении гаражом",
        "code": CERTIFICATE_CODE,
        "chairman_only": True,
        "title": "СПРАВКА",
        "recipient": "garage",
        # Справка — «по месту требования»: куда её понесут, решает
        # собственник, конкретный орган не называем.
        "addressee_html": "<p>по месту требования</p>",
        "body_html": (
            "<p>Выдана {{ФИО_кому}} ({{дата_рождения}} г. р.; паспорт {{паспорт}}; адрес регистрации: "
            "{{адрес_регистрации}}) в том, что {{он_она}} {{членство}}владеет {{владение}} на территории "
            "{{кооператив_кратко}} по адресу: {{юр_адрес}}, {{сведения_о_гараже}}.</p>"
            "<p>{{нет_задолженности}}</p>"
            "<p>Справка выдана для предъявления по месту требования.</p>"
        ),
    },
    {
        "name": "Заявление о вступлении в кооператив",
        "is_public": True,
        "title": "ЗАЯВЛЕНИЕ",
        "recipient": "none",
        "show_number": False,
        "show_signature": False,
        # Бланк: вступающего обычно ещё нет в реестре — данные от руки.
        "addressee_html": _TO_BOARD + (
            "<p>от __________________________________<br>__________________________________<br>"
            "<span style=\"font-size: 9pt\">(фамилия, имя, отчество полностью)</span></p>"
            "<p>паспорт: серия ______ № ____________<br>выдан ______________________________<br>"
            "дата выдачи ____________</p>"
            "<p>адрес регистрации: ________________________<br>__________________________________</p>"
            "<p>телефон: ________________________<br>эл. почта: ______________________</p>"
        ),
        "body_html": (
            _FILL + "Прошу принять меня в члены {{кооператив_кратко}} в связи с приобретением гаража (бокса) "
            "№ ______, площадью ______ м², расположенного на территории кооператива по адресу: {{юр_адрес}}.</p>"
            + _FILL + "Основание владения гаражом: ______________________________ (договор "
            "купли-продажи, дарения, свидетельство о праве на наследство, выписка из ЕГРН и т. п.).</p>"
            "<p>С Уставом кооператива ознакомлен(а), обязуюсь его соблюдать, выполнять решения общего "
            "собрания и правления, своевременно уплачивать членские и иные взносы.</p>"
            "<p style=\"text-indent: 0\">Приложение: копия документа, подтверждающего право на гараж.</p>"
            + _APPLICANT_SIGN.replace("{{ФИО_кратко}}", "______________________")
        ),
    },
    {
        "name": "Заявление о выходе из кооператива",
        "is_public": True,
        "title": "ЗАЯВЛЕНИЕ",
        "recipient": "person",
        "show_number": False,
        "show_signature": False,
        "addressee_html": _TO_BOARD + _FROM_PERSON,
        "body_html": (
            _FILL + "Прошу исключить меня из членов {{кооператив_кратко}} по собственному желанию в связи с "
            "______________________________ (продажей, дарением гаража № {{гараж}} и т. п.).</p>"
            + _FILL + "Новый владелец гаража: ______________________________ "
            "(фамилия, имя, отчество, телефон).</p>"
            "<p>Обязуюсь погасить задолженность по взносам и платежам перед кооперативом, если она "
            "имеется на дату выхода.</p>"
            + _APPLICANT_SIGN
        ),
    },
    {
        "name": "Заявление на подключение к электрической сети",
        "is_public": True,
        "title": "ЗАЯВЛЕНИЕ",
        "recipient": "garage",
        "show_number": False,
        "show_signature": False,
        "addressee_html": _TO_BOARD + _FROM_PERSON,
        "body_html": (
            "<p>Прошу подключить принадлежащий мне гараж (бокс) № {{гараж}} (доля в праве {{доля}}) к "
            "электрической сети {{кооператив_кратко}}.</p>"
            + _FILL + "Требуемая мощность: ______ кВт; напряжение: 220 В / 380 В (нужное подчеркнуть). "
            "Назначение: освещение, ______________________.</p>"
            "<p>Обязуюсь установить прибор учёта электроэнергии, отвечающий установленным требованиям, в "
            "месте, согласованном с правлением, оплатить подключение, своевременно передавать показания "
            "и оплачивать потреблённую электроэнергию, соблюдать правила электро- и пожарной "
            "безопасности.</p>"
            + _APPLICANT_SIGN
        ),
    },
    {
        "name": "Уведомление о задолженности",
        "title": "УВЕДОМЛЕНИЕ О ЗАДОЛЖЕННОСТИ",
        "recipient": "person",
        "addressee_html": "<p>{{ФИО_кому}}</p><p>{{адрес}}</p>",
        "body_html": (
            "<p style=\"text-align: center; text-indent: 0\">{{уважаемый}} {{имя_отчество}}!</p>"
            "<p>Правление {{кооператив_кратко}} уведомляет, что по данным учёта кооператива на {{сегодня}} "
            "за Вами числится задолженность по взносам и платежам в размере {{задолженность}} руб. "
            "(гараж № {{гараж}}).</p>"
            "<p>Просим погасить задолженность в течение 30 дней с даты получения настоящего уведомления.</p>"
            + _PAY_DETAILS +
            "<p>Если задолженность не будет погашена в указанный срок, кооператив будет вынужден обратиться "
            "в суд с требованием о её взыскании, а также пени и судебных расходов.</p>"
            "<p>Если оплата уже произведена, просим сообщить об этом правлению: тел. {{телефон_кооператива}}, "
            "{{email}}.</p>"
        ),
    },
    {
        "name": "Объявление об общем собрании",
        "title": "ОБЪЯВЛЕНИЕ",
        "recipient": "none",
        "show_number": False,
        "signer_role": "Правление",
        "body_html": (
            "<p style=\"text-align: center; text-indent: 0\"><b>Уважаемые члены {{кооператив_кратко}}!</b></p>"
            "<p>Правление кооператива сообщает о проведении очередного общего собрания членов кооператива.</p>"
            "<p>Дата и время: «___» ____________ {{год}} г., ___ ч ___ мин.<br>"
            "Место проведения: ______________________________.<br>"
            "Регистрация участников — с ___ ч ___ мин.</p>"
            "<p><b>Повестка дня:</b></p>"
            "<ol><li>Отчёт правления о работе за {{год}} год.</li>"
            "<li>Отчёт ревизионной комиссии.</li>"
            "<li>Утверждение сметы доходов и расходов и размера взносов.</li>"
            "<li>Разное.</li></ol>"
            "<p>При себе необходимо иметь паспорт. Представитель члена кооператива — "
            "доверенность. С материалами к собранию можно ознакомиться у правления и на сайте {{сайт}}.</p>"
        ),
    },
    {
        "name": "Протокол заседания правления",
        "title": "ПРОТОКОЛ № ___ заседания правления",
        "recipient": "none",
        "show_number": False,
        "signer_role": "Председатель правления",
        "body_html": (
            "<p style=\"text-indent: 0\">Дата проведения: {{сегодня}}.<br>"
            "Место проведения: ______________________________.</p>"
            "<p style=\"text-indent: 0\">Присутствовали члены правления:</p>"
            "<ol><li>__________________</li><li>__________________</li><li>__________________</li></ol>"
            "<p style=\"text-indent: 0\">Кворум имеется. Председатель заседания — {{председатель}}, "
            "секретарь — __________________.</p>"
            "<p style=\"text-indent: 0\"><b>Повестка дня:</b></p>"
            "<ol><li>__________________</li></ol>"
            "<p style=\"text-indent: 0\"><b>1. По первому вопросу</b> слушали: __________________.<br>"
            "Решили: __________________.<br>"
            "Голосовали: «за» — ___, «против» — ___, «воздержались» — ___. Решение принято.</p>"
            "<p style=\"text-indent: 0; text-align: right\">Секретарь заседания ______________ /______________/</p>"
        ),
    },
    {
        "name": "Объявление",
        "title": "ОБЪЯВЛЕНИЕ",
        "recipient": "none",
        "show_number": False,
        "signer_role": "Правление",
        "body_html": (
            "<p style=\"text-align: center; text-indent: 0\"><b>Уважаемые члены {{кооператив_кратко}}!</b></p>"
            "<p>Текст объявления.</p>"
            "<p>Справки: тел. {{телефон_кооператива}}, {{email}}, {{сайт}}.</p>"
        ),
    },
    {
        "name": "Сайт и группы в соцсетях",
        "title": "КООПЕРАТИВ В ИНТЕРНЕТЕ",
        "recipient": "none",
        "show_number": False,
        "show_signature": False,
        "body_html": (
            "<p style=\"text-align: center; text-indent: 0\">Новости кооператива, объявления, начисления "
            "и показания счётчиков, квитанции, вопросы правлению — на сайте и в группах:</p>"
            "<p style=\"text-align: center; text-indent: 0; font-size: 18pt\"><b>{{сайт}}</b></p>"
            "<p style=\"text-align: center; text-indent: 0\">{{соцсети}}</p>"
            "<p style=\"text-align: center; text-indent: 0\">Электронная почта: {{email}}<br>"
            "Телефон: {{телефон_кооператива}}</p>"
            "<p style=\"text-align: center; text-indent: 0\">Логин и пароль для входа в личный кабинет "
            "выдаёт правление.</p>"
        ),
    },
    {
        "name": "Регистрационные данные и реквизиты",
        "title": "РЕГИСТРАЦИОННЫЕ ДАННЫЕ И РЕКВИЗИТЫ",
        "recipient": "none",
        "show_number": False,
        "body_html": (
            "<table><tbody>"
            "<tr><td>Полное наименование</td><td>{{кооператив}}</td></tr>"
            "<tr><td>Краткое наименование</td><td>{{кооператив_кратко}}</td></tr>"
            "<tr><td>ИНН / КПП</td><td>{{ИНН}} / {{КПП}}</td></tr>"
            "<tr><td>ОГРН</td><td>{{ОГРН}}</td></tr>"
            "<tr><td>Дата регистрации</td><td>{{дата_регистрации}}</td></tr>"
            "<tr><td>Юридический адрес</td><td>{{юр_адрес}}</td></tr>"
            "<tr><td>Почтовый адрес</td><td>{{почтовый_адрес}}</td></tr>"
            "<tr><td>Расчётный счёт</td><td>{{расчётный_счёт}}</td></tr>"
            "<tr><td>Банк</td><td>{{банк}}</td></tr>"
            "<tr><td>БИК</td><td>{{БИК}}</td></tr>"
            "<tr><td>Корреспондентский счёт</td><td>{{корр_счёт}}</td></tr>"
            "<tr><td>Председатель</td><td>{{председатель}}</td></tr>"
            "<tr><td>Телефон</td><td>{{телефон_кооператива}}</td></tr>"
            "<tr><td>Электронная почта</td><td>{{email}}</td></tr>"
            "<tr><td>Сайт</td><td>{{сайт}}</td></tr>"
            "</tbody></table>"
        ),
    },
]


def add_default_templates() -> int:
    """Добавить стандартные шаблоны, которых ещё нет (по названию)."""
    templates = database.db_session.query(DocumentTemplate).all()
    names = {t.name for t in templates}
    codes = {t.code for t in templates if t.code}
    added = 0
    for order, data in enumerate(DEFAULT_TEMPLATES):
        if data["name"] in names or data.get("code") in codes:
            continue
        database.db_session.add(_default_template(order, data))
        added += 1
    return added


def _default_template(order: int, data: dict) -> DocumentTemplate:
    return DocumentTemplate(**{
        "show_number": True, "show_signature": True, "signer_role": "Председатель", "addressee_html": "",
        "chairman_only": False, "is_public": False, "sort_order": order * 10, **data,
    })


def certificate_template() -> DocumentTemplate:
    """Шаблон справки о владении гаражом (по нему оформляются запросы из
    кабинета) — создаётся из стандартного, если его ещё нет."""
    template = database.db_session.query(DocumentTemplate).filter_by(code=CERTIFICATE_CODE).first()
    if template is None:
        order, data = next((i, d) for i, d in enumerate(DEFAULT_TEMPLATES) if d.get("code") == CERTIFICATE_CODE)
        template = _default_template(order, data)
        database.db_session.add(template)
        database.db_session.flush()
    return template


def visible_documents(person_id: int | None = None, garage_id: int | None = None,
                      code: str | None = None) -> list[IssuedDocument]:
    """Для карточек человека/гаража и кабинета — правлению все, члену —
    адресованные ему."""
    query = database.db_session.query(IssuedDocument)
    if person_id is not None:
        query = query.filter(IssuedDocument.person_id == person_id)
    if garage_id is not None:
        query = query.filter(IssuedDocument.garage_id == garage_id)
    if code is not None:
        query = query.join(DocumentTemplate).filter(DocumentTemplate.code == code)
    if not is_board():
        if g.user.person_id is None:
            return []
        query = query.filter(IssuedDocument.person_id == g.user.person_id)
    return query.order_by(IssuedDocument.issued_at.desc(), IssuedDocument.id.desc()).all()


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------

def next_number(today: dt.date) -> str:
    counter = database.db_session.get(DocumentCounter, today.year)
    if counter is None:
        counter = DocumentCounter(year=today.year, last_number=0)
        database.db_session.add(counter)
    counter.last_number += 1
    return f"{today.year}-{counter.last_number}"


def _template_or_404(template_id) -> DocumentTemplate:
    template = database.db_session.get(DocumentTemplate, template_id) if template_id else None
    if template is None:
        abort(404)
    return template


def _document_or_404(document_id: int) -> IssuedDocument:
    document = database.db_session.get(IssuedDocument, document_id)
    if document is None:
        abort(404)
    if not (is_board() or (g.user.person_id is not None and g.user.person_id == document.person_id)):
        abort(403)
    return document


def _clean(raw: str | None) -> tuple[str, bool]:
    """(санитизированный HTML, слишком_длинный)."""
    raw = raw or ""
    return sanitize_html(raw), len(raw) > MAX_HTML_LENGTH


def _ownership_choices() -> list[GarageOwnership]:
    return sorted(
        database.db_session.query(GarageOwnership).all(),
        key=lambda o: (o.person.full_name.lower(), o.garage.number),
    )


def _person_choices() -> list[Person]:
    return sorted(database.db_session.query(Person).all(), key=lambda p: p.full_name.lower())


@bp.app_template_global("document_recipient_label")
def recipient_label(recipient: str) -> str:
    return _(RECIPIENTS.get(recipient, recipient))


# ---------------------------------------------------------------------------
# Библиотека и журнал
# ---------------------------------------------------------------------------

@bp.route("/")
@roles_required(RoleEnum.BOARD)
def index():
    templates = (
        database.db_session.query(DocumentTemplate)
        .order_by(DocumentTemplate.is_archived, DocumentTemplate.sort_order, DocumentTemplate.name)
        .all()
    )
    missing_defaults = [d["name"] for d in DEFAULT_TEMPLATES
                        if d["name"] not in {t.name for t in templates}
                        and d.get("code") not in {t.code for t in templates if t.code}]
    open_requests = (
        database.db_session.query(CertificateRequest)
        .filter(CertificateRequest.status.in_([CertificateRequestStatus.REQUESTED, CertificateRequestStatus.READY]))
        .order_by(CertificateRequest.created_at)
        .all()
    )
    documents = (
        database.db_session.query(IssuedDocument)
        .order_by(IssuedDocument.issued_at.desc(), IssuedDocument.id.desc())
        .limit(500)
        .all()
    )
    return render_template(
        "documents/index.html", templates=templates, documents=documents, missing_defaults=missing_defaults,
        open_requests=open_requests,
    )


@bp.route("/templates/defaults", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_defaults():
    # Шаблоны — заготовки, не действия от имени кооператива: в журнал
    # аудита не пишутся (там только важное — см. issued_document.*).
    added = add_default_templates()
    database.db_session.commit()
    flash(_("Добавлено стандартных шаблонов: {n}.", n=added), "success")
    return redirect(url_for("documents.index"))


# ---------------------------------------------------------------------------
# Шаблоны
# ---------------------------------------------------------------------------

@bp.route("/templates/new", methods=["GET", "POST"])
@bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit_template(template_id=None):
    template = _template_or_404(template_id) if template_id else None
    if template is None and request.args.get("copy_of"):
        source = _template_or_404(request.args.get("copy_of", type=int))
        draft = DocumentTemplate(
            name=f"{source.name} (копия)", title=source.title, addressee_html=source.addressee_html,
            body_html=source.body_html, recipient=source.recipient, show_number=source.show_number,
            show_signature=source.show_signature, signer_role=source.signer_role, sort_order=source.sort_order,
            chairman_only=source.chairman_only, is_public=source.is_public,
        )
    else:
        draft = template or DocumentTemplate(
            name="", title="", addressee_html="", body_html="", recipient="none",
            show_number=True, show_signature=True, signer_role="Председатель", sort_order=0, chairman_only=False,
            is_public=False,
        )

    if request.method == "POST":
        f = request.form
        body_html, body_too_long = _clean(f.get("body_html"))
        addressee_html, addressee_too_long = _clean(f.get("addressee_html"))
        draft.name = (f.get("name") or "").strip()[:200]
        draft.title = (f.get("title") or "").strip()[:200]
        draft.recipient = f.get("recipient") if f.get("recipient") in RECIPIENTS else "none"
        draft.show_number = bool(f.get("show_number"))
        draft.show_signature = bool(f.get("show_signature"))
        draft.chairman_only = bool(f.get("chairman_only"))
        draft.is_public = bool(f.get("is_public"))
        draft.signer_role = (f.get("signer_role") or "").strip()[:100] or "Председатель"
        draft.sort_order = f.get("sort_order", type=int) or 0
        draft.body_html = body_html
        draft.addressee_html = addressee_html if has_text(addressee_html) else ""
        error = None
        if not draft.name:
            error = _("Укажите название шаблона.")
        elif body_too_long or addressee_too_long:
            error = _("Текст шаблона слишком длинный.")
        elif not has_text(body_html):
            error = _("Текст шаблона пуст.")
        if error:
            flash(error, "danger")
            if template is not None:
                database.db_session.expunge(template)
            return _render_template_form(draft, template)
        if template is None:
            database.db_session.add(draft)
        database.db_session.commit()
        unknown = unknown_placeholders(draft.title, draft.addressee_html, draft.body_html)
        if unknown:
            flash(_("Неизвестные подстановки останутся в тексте как есть: {keys}.",
                    keys=", ".join("{{" + k + "}}" for k in unknown)), "warning")
        flash(_("Шаблон «{name}» сохранён.", name=draft.name), "success")
        return redirect(url_for("documents.index"))

    return _render_template_form(draft, template)


def _render_template_form(draft: DocumentTemplate, template: DocumentTemplate | None):
    coop, chairman = _coop_and_chairman()
    return render_template(
        "documents/template_form.html", draft=draft, template=template, coop=coop, chairman=chairman,
        recipients=RECIPIENTS, variable_groups=variable_groups(), today=dt.date.today(),
    )


@bp.route("/templates/<int:template_id>/archive", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def archive_template(template_id):
    template = _template_or_404(template_id)
    template.is_archived = not template.is_archived
    database.db_session.commit()
    return redirect(url_for("documents.index"))


# ---------------------------------------------------------------------------
# Документы
# ---------------------------------------------------------------------------

def _recipient_from(form, recipient: str):
    """(person, garage) из формы: owner="<person_id>:<garage_id>" или person_id."""
    if recipient == "garage":
        owner = form.get("owner")
        if not owner and (form.get("person_id") or form.get("garage_id")):
            owner = next((
                f"{o.person_id}:{o.garage_id}" for o in _ownership_choices()
                if (not form.get("person_id") or str(o.person_id) == form.get("person_id"))
                and (not form.get("garage_id") or str(o.garage_id) == form.get("garage_id"))
            ), None)
        if owner and ":" in owner:
            person_id, garage_id = owner.split(":", 1)
            ownership = (
                database.db_session.query(GarageOwnership)
                .filter_by(person_id=int(person_id or 0), garage_id=int(garage_id or 0)).first()
                if person_id.isdigit() and garage_id.isdigit() else None
            )
            if ownership is not None:
                return ownership.person, ownership.garage
        return None, None
    if recipient == "person":
        person_id = form.get("person_id", type=int)
        person = database.db_session.get(Person, person_id) if person_id else None
        return person, None
    return None, None


def _needs_recipient(recipient: str, person, garage) -> bool:
    return (recipient != "none" and person is None) or (recipient == "garage" and garage is None)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    """Составить документ: по шаблону (template_id), копией другого
    документа (copy_of) или справку по запросу из кабинета (request_id)."""
    form = request.form if request.method == "POST" else request.args
    source = cert_request = None
    if form.get("request_id"):
        cert_request = database.db_session.get(CertificateRequest, form.get("request_id", type=int))
        if cert_request is None or cert_request.status != CertificateRequestStatus.REQUESTED:
            flash(_("Запрос не найден или уже обработан."), "warning")
            return redirect(url_for("documents.index", _anchor="certificate-requests"))
        template = certificate_template()
        recipient = template.recipient
    elif form.get("copy_of"):
        source = _document_or_404(form.get("copy_of", type=int))
        template = source.template
        recipient = template.recipient if template else ("garage" if source.garage_id else
                                                         "person" if source.person_id else "none")
    else:
        template = _template_or_404(form.get("template_id", type=int))
        recipient = template.recipient
    if template is not None and template.chairman_only and not is_chairman():
        flash(_("Документы по шаблону «{name}» составляет только председатель.", name=template.name), "warning")
        return redirect(url_for("documents.index"))

    if cert_request is not None:
        person, garage = _recipient_from({"owner": f"{cert_request.person_id}:{cert_request.garage_id or 0}"}, recipient)
        if person is None and request.method == "GET":
            flash(_("Заявитель уже не числится собственником этого гаража."), "warning")
    elif source is not None and "owner" not in form and "person_id" not in form:
        person, garage = source.person, source.garage
    else:
        person, garage = _recipient_from(form, recipient)
    need_recipient = _needs_recipient(recipient, person, garage)
    coop, chairman = _coop_and_chairman()

    if request.method == "POST":
        body_html, body_too_long = _clean(form.get("body_html"))
        addressee_html, addressee_too_long = _clean(form.get("addressee_html"))
        try:
            issued_at = dt.date.fromisoformat(form.get("issued_at") or "")
        except ValueError:
            issued_at = None
        title = (form.get("title") or "").strip()[:200]
        error = None
        if need_recipient:
            error = _("Выберите адресата.")
        elif issued_at is None:
            error = _("Укажите дату документа.")
        elif body_too_long or addressee_too_long:
            error = _("Текст документа слишком длинный.")
        elif not has_text(body_html):
            error = _("Текст документа пуст.")
        if error:
            flash(error, "danger")
            return _render_create(template, source, recipient, person, garage, coop, chairman, cert_request,
                                  title=title, addressee_html=addressee_html, body_html=body_html,
                                  issued_at=issued_at or dt.date.today())
        show_number = template.show_number if template else bool(source and source.number)
        document = IssuedDocument(
            template_id=template.id if template else None,
            number=next_number(issued_at) if show_number else None,
            title=title, addressee_html=addressee_html if has_text(addressee_html) else "",
            body_html=body_html, issued_at=issued_at,
            person_id=person.id if person else None, garage_id=garage.id if garage else None,
            show_signature=template.show_signature if template else source.show_signature,
            signer_role=template.signer_role if template else source.signer_role,
            signer_person_id=chairman.id if chairman else None,
            created_by_user_id=g.user.id,
        )
        database.db_session.add(document)
        database.db_session.flush()
        doc_name = template.name if template else document.title
        audit.record(
            "issued_document.create", entity_type="issued_document", entity_id=document.id,
            summary=f"Составлен документ «{doc_name}»"
                    + (f" № {document.number}" if document.number else "")
                    + (f": {person.full_name}" if person else "")
                    + (f", гараж № {garage.number}" if garage else ""),
        )
        if cert_request is not None:
            from .certificates import notify_requester
            cert_request.status = CertificateRequestStatus.READY
            cert_request.document_id = document.id
            cert_request.ready_at = dt.datetime.now()
            notify_requester(
                cert_request, f"Справка по гаражу № {cert_request.garage_number} готова",
                f"Справка № {document.number or ''} о владении гаражом № {cert_request.garage_number} готова — "
                "заберите подписанный экземпляр у председателя.",
            )
        database.db_session.commit()
        flash(_("Документ сохранён в журнале."), "success")
        return redirect(url_for("documents.detail", document_id=document.id))

    # Дата из адреса — пересчёт текста по новой дате ({{сегодня}}, {{сегодня+N}}).
    try:
        issued_at = dt.date.fromisoformat(request.args.get("issued_at") or "")
    except ValueError:
        issued_at = None
    return _render_create(template, source, recipient, person, garage, coop, chairman, cert_request,
                          issued_at=issued_at)


def _render_create(template, source, recipient, person, garage, coop, chairman, cert_request=None,
                   title=None, addressee_html=None, body_html=None, issued_at=None):
    issued_at = issued_at or dt.date.today()
    missing: set[str] = set()
    need_recipient = _needs_recipient(recipient, person, garage)
    if not need_recipient and body_html is None:
        if source is not None and (template is None or (person == source.person and garage == source.garage)):
            title, addressee_html, body_html = source.title, source.addressee_html, source.body_html
        else:
            ctx = Context(issued_at, person, garage)
            title = fill_text(template.title, ctx, missing)
            addressee_html = fill(template.addressee_html, ctx, missing)
            body_html = fill(template.body_html, ctx, missing)
    labels = {key: desc for key, desc, _g, _f in VARIABLES}
    debt = person_debt(person) if person is not None and not need_recipient else None
    if cert_request is not None:
        keep = {"request_id": cert_request.id}
    elif source is not None:
        keep = {"copy_of": source.id}
    else:
        keep = {"template_id": template.id}
    return render_template(
        "documents/create.html", template=template, source=source, recipient=recipient, keep=keep,
        cert_request=cert_request, person=person, garage=garage, coop=coop, chairman=chairman,
        need_recipient=need_recipient, title=title or "", addressee_html=addressee_html or "",
        body_html=body_html or "", issued_at=issued_at,
        missing=sorted(labels.get(k, k) for k in missing), debt=debt,
        show_number=template.show_number if template else bool(source and source.number),
        show_signature=template.show_signature if template else source.show_signature,
        signer_role=template.signer_role if template else source.signer_role,
        ownerships=_ownership_choices() if recipient == "garage" and cert_request is None else [],
        persons=_person_choices() if recipient == "person" else [],
    )


@bp.route("/<int:document_id>")
@login_required
def detail(document_id):
    document = _document_or_404(document_id)
    coop, _chairman = _coop_and_chairman()
    return render_template("documents/detail.html", document=document, coop=coop)


@bp.route("/<int:document_id>/pdf")
@login_required
def pdf(document_id):
    document = _document_or_404(document_id)
    coop, _chairman = _coop_and_chairman()
    filename = f"document-{document.number or document.id}.pdf"
    return _render_pdf_or_fallback("documents/pdf.html", filename, "documents/detail.html",
                                   document=document, coop=coop)


@bp.route("/<int:document_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(document_id):
    document = _document_or_404(document_id)
    if document.template is not None and document.template.chairman_only and not is_chairman():
        flash(_("Документы по шаблону «{name}» составляет только председатель.", name=document.template.name),
              "warning")
        return redirect(url_for("documents.detail", document_id=document.id))
    audit.record(
        "issued_document.delete", entity_type="issued_document", entity_id=document.id,
        summary=f"Удалён из журнала документ «{document.title}»"
                + (f" № {document.number}" if document.number else "")
                + (f" от {_date(document.issued_at)}"),
    )
    # Справка по запросу удалена — запрос снова ждёт оформления.
    for req in database.db_session.query(CertificateRequest).filter_by(document_id=document.id):
        req.document_id = None
        if req.status == CertificateRequestStatus.READY:
            req.status = CertificateRequestStatus.REQUESTED
            req.ready_at = None
    database.db_session.delete(document)
    database.db_session.commit()
    flash(_("Документ удалён из журнала (его номер не используется повторно)."), "success")
    return redirect(url_for("documents.index"))


# ---------------------------------------------------------------------------
# Общедоступные бланки (без входа)
# ---------------------------------------------------------------------------

def public_templates() -> list[DocumentTemplate]:
    return (
        database.db_session.query(DocumentTemplate)
        .filter(DocumentTemplate.is_public.is_(True), DocumentTemplate.is_archived.is_(False))
        .order_by(DocumentTemplate.sort_order, DocumentTemplate.name)
        .all()
    )


def _public_template_or_404(template_id: int) -> DocumentTemplate:
    template = database.db_session.get(DocumentTemplate, template_id)
    if template is None or not template.is_public or template.is_archived:
        abort(404)
    return template


def _public_context(template: DocumentTemplate) -> dict:
    """Пустой бланк: данные человека/гаража/долга — прочерками для
    заполнения от руки, расчётный счёт — тоже (Context(public=True))."""
    today = dt.date.today()
    ctx = Context(today, public=True)
    coop, chairman = ctx.coop, ctx.chairman
    return {
        "template": template, "coop": coop, "chairman": chairman, "today": today,
        "title": fill_text(template.title, ctx),
        "addressee_html": fill(template.addressee_html, ctx),
        "body_html": fill(template.body_html, ctx),
    }


@bp.route("/public/")
def public_list():
    return render_template("documents/public_list.html", templates=public_templates())


@bp.route("/public/<int:template_id>")
def public_view(template_id):
    return render_template("documents/public_view.html", **_public_context(_public_template_or_404(template_id)))


@bp.route("/public/<int:template_id>/pdf")
def public_pdf(template_id):
    template = _public_template_or_404(template_id)
    return _render_pdf_or_fallback("documents/public_pdf.html", f"blank-{template.id}.pdf",
                                   "documents/public_view.html", **_public_context(template))
