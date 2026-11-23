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
Справку из личного кабинета запрашивают заявлением в правление вида
«справка о владении гаражом» (app/applications.py): председатель
оформляет её здесь же — create?application_id=…, документ привязывается
к заявлению (Application.document_id), заявление закрывается ответом. Документы, адресованные человеку, он видит сам
(кабинет, карточки), остальное — только правление.

Уведомления о задолженности с выпиской по годам и иски — в «Должниках»
(app/legal_docs.py), здесь — простое уведомление-письмо.
"""
import datetime as dt
import re
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from markupsafe import Markup, escape
from sqlalchemy import or_

from . import audit
from . import database
from . import name_declension
from .accounting import balance, electricity_account_number, get_primary_bank_account
from .applications import sanitize_html, has_text, MAX_HTML_LENGTH
from .auth import login_required, roles_required
from .i18n import translate as _
from .legal_docs import _render_pdf_or_fallback, _coop_and_chairman
from .models import garage_sort_key, short_name_of
from .models import (
    Application, ApplicationKind, ApplicationStatus, Counterparty, DocumentCounter, DocumentTemplate, ElectricitySettings,
    Garage, GarageOwnership, IssuedDocument, MemberAccount, Person, RoleEnum,
)
from .permissions import is_board, is_chairman

bp = Blueprint("documents", __name__, url_prefix="/documents")


def split_doc_title(title: str | None) -> tuple[str, str]:
    """Заголовок на бланке — вид документа отдельной строкой прописными,
    ниже пояснение: «ДОГОВОР о подключении…» → («ДОГОВОР», «о подключении…»),
    «Ответ на заявление» → («ОТВЕТ», «на заявление»). «№ …» после вида
    остаётся в первой строке («ПРОТОКОЛ № ___»). Заголовок целиком
    прописными («КООПЕРАТИВ В ИНТЕРНЕТЕ») — не делится: это не вид
    документа с пояснением."""
    words = (title or "").split()
    if not words:
        return "", ""
    if len(words) > 1 and not any(ch.islower() for ch in " ".join(words)):
        return " ".join(words), ""
    head = [words[0]]
    rest = words[1:]
    if rest and rest[0] == "№":
        head += rest[:2]
        rest = rest[2:]
    return " ".join(head).upper(), " ".join(rest)


bp.add_app_template_global(split_doc_title, "split_doc_title")

RECIPIENTS = {
    "none": "без адресата (объявление, информационный лист)",
    "person": "человек из реестра",
    "garage": "собственник гаража",
    "sale": "продавец и покупатель гаража",
    "contractor": "контрагент (организация, ИП, самозанятый)",
}

BLANK = "________"
CERTIFICATE_CODE = "garage_certificate"
# Заявление на подключение к электросети (подаётся из кабинета) и договор,
# который по нему оформляет правление (documents.create?application_id=…).
POWER_APPLICATION_CODE = "power_application"
POWER_CONTRACT_CODE = "power_contract"
# Договор купли-продажи гаража между собственником и покупателем:
# оформляет и заверяет печатью председатель (recipient="sale").
SALE_CONTRACT_CODE = "sale_contract"

# Прочерки в шапке (правая колонка — «от кого», адрес, паспорт, телефон):
# пропуск должен вмещать то, что впишут от руки, адрес — в две строки.
_ADDRESS_BLANK = "_" * 24
HEADER_BLANKS = {
    "паспорт": "_" * 22,
    "адрес": _ADDRESS_BLANK + "<br>" + _ADDRESS_BLANK,
    "адрес_регистрации": _ADDRESS_BLANK + "<br>" + _ADDRESS_BLANK,
    "юр_адрес": _ADDRESS_BLANK + "<br>" + _ADDRESS_BLANK,
    "почтовый_адрес": _ADDRESS_BLANK + "<br>" + _ADDRESS_BLANK,
    "телефон": "_" * 24,
    # «от ________» — ФИО от руки на восьми подчёркиваниях не уместить.
    **{key: "_" * 24 for key in ("ФИО", "ФИО_кому", "ФИО_от_кого", "ФИО_кратко")},
}
# В шапке телефоны — по одному на строке (в тексте — через запятую).
HEADER_MULTILINE = {"телефон": lambda c: c.person_attr(lambda p: "\n".join(ph.number for ph in p.phones))}


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
        key=lambda o: garage_sort_key(o.garage),
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


def _named(ctx) -> str:
    """«именуемый» / «именуемая» в преамбуле договора — по отчеству."""
    if ctx.person is None:
        return ""
    return {"male": "именуемый", "female": "именуемая"}.get(
        name_declension.gender(ctx.person.full_name), "именуемый(ая)")


def _current_meter(ctx):
    # Как garages._current_meter: актуальный — последний по дате установки.
    if ctx.garage is None or not ctx.garage.meters:
        return None
    return max(ctx.garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id))


def _number(value: Decimal) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _meter_reading(ctx) -> str:
    """Последние показания актуального счётчика (или начальные при
    установке) — «начальные показания» в договоре на электроэнергию."""
    meter = _current_meter(ctx)
    if meter is None:
        return ""
    last = max(meter.readings, key=lambda r: (r.reading_date, r.id), default=None)
    value = last.reading if last is not None else meter.initial_reading
    return "" if value is None else _number(value)


def _self_consumption(ctx) -> str:
    """«2 Вт — 1,46 кВт·ч в месяц (17,52 кВт·ч в год)» — как начисляется
    (accounting.meter_self_consumption_kwh)."""
    settings = database.db_session.query(ElectricitySettings).first()
    watts = settings.meter_self_consumption_w if settings else Decimal("2")
    year = watts * 24 * 365 / 1000
    return (f"{_number(watts)} Вт — {_number((year / 12).quantize(Decimal('0.01')))} кВт·ч в месяц "
            f"({_number(year.quantize(Decimal('0.01')))} кВт·ч в год)")


_MONTHS_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                    "сентября", "октября", "ноября", "декабря")


def _dues_due(ctx) -> str:
    """«31 марта» — единый срок уплаты взносов и платежей (реквизиты
    кооператива, Cooperative.dues_due_day/month)."""
    coop = ctx.coop
    if not coop or not coop.dues_due_day or not coop.dues_due_month:
        return ""
    return f"{coop.dues_due_day} {_MONTHS_GENITIVE[coop.dues_due_month - 1]}"


def _outside_loss(ctx) -> str:
    # Только чтение: get_electricity_settings() создала бы запись при
    # предпросмотре шаблона.
    settings = database.db_session.query(ElectricitySettings).first()
    return _number(settings.outside_line_loss_percent if settings else Decimal("2.33"))


def _counterparty_contract(ctx) -> str:
    cp = ctx.counterparty
    if cp is None or not (cp.contract_number or cp.contract_date):
        return ""
    parts = [f"№ {cp.contract_number}"] if cp.contract_number else []
    if cp.contract_date:
        parts.append(f"от {_date(cp.contract_date)}")
    return " ".join(parts)


def _sale_subject(ctx) -> str:
    """«гараж (бокс) № 42» или «1/2 доли в праве собственности на гараж
    (бокс) № 42» — что продаёт продавец (по его доле в праве)."""
    if ctx.garage is None:
        return ""
    if ctx.ownership is None or ctx.ownership.share == 1:
        return f"гараж (бокс) № {ctx.garage.number}"
    return f"{_share(ctx.ownership.share)} доли в праве собственности на гараж (бокс) № {ctx.garage.number}"


def _party_variables(prefix: str, title: str, getter) -> list:
    """Подстановки стороны договора купли-продажи: {{продавец}},
    {{продавец_паспорт}} … — getter(ctx) → Person или None (не из
    реестра — прочерки)."""
    def attr(fn):
        return lambda c: fn(p) if (p := getter(c)) is not None else ""
    group = "Купля-продажа"
    return [
        (prefix, f"{title}: ФИО полностью", group, attr(lambda p: p.full_name)),
        (f"{prefix}_кратко", f"{title}: «Петрова А.И.»", group, attr(lambda p: p.short_name)),
        (f"{prefix}_именуемый", f"«именуемый» / «именуемая» по отчеству ({title.lower()})", group,
         attr(lambda p: {"male": "именуемый", "female": "именуемая"}.get(
             name_declension.gender(p.full_name), "именуемый(ая)"))),
        (f"{prefix}_дата_рождения", f"{title}: дата рождения", group, attr(lambda p: _date(p.birth_date))),
        (f"{prefix}_паспорт", f"{title}: паспорт", group, attr(_passport)),
        (f"{prefix}_адрес_регистрации", f"{title}: адрес регистрации", group,
         attr(lambda p: p.registration_address or "")),
        (f"{prefix}_телефон", f"{title}: телефоны", group,
         attr(lambda p: ", ".join(ph.number for ph in p.phones))),
    ]


class Context:
    """Данные для подстановок: кооператив, председатель, основной
    расчётный счёт, дата документа и (если есть) адресат."""

    def __init__(self, today: dt.date, person: Person | None = None, garage: Garage | None = None,
                 public: bool = False, buyer: Person | None = None, counterparty: Counterparty | None = None):
        self.coop, self.chairman = _coop_and_chairman()
        # Общедоступный бланк — без банковских реквизитов (как и публичная
        # страница кооператива): в бланке прочерк.
        self.bank = None if public else get_primary_bank_account()
        self.today = today
        self.person = person
        self.garage = garage
        # Договор купли-продажи: продавец — person (собственник garage),
        # покупатель — buyer; любой из них может быть не из реестра.
        self.buyer = buyer
        # Письмо контрагенту (recipient="contractor").
        self.counterparty = counterparty
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

    def counterparty_attr(self, name: str) -> str:
        return (getattr(self.counterparty, name, None) or "") if self.counterparty else ""


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
    ("срок_оплаты", "единый срок уплаты взносов и платежей в году: «31 марта» (реквизиты кооператива)",
     "Кооператив", _dues_due),
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
    ("лицевой_счёт_гаража", "лицевой счёт гаража (электроэнергия) — указывают при оплате", "Гараж",
     # Счёта ещё нет (заводится с гаражом) — номер по той же формуле.
     lambda c: c.garage_attr(lambda gr: gr.account.account_number if gr.account
                             else electricity_account_number(gr.id))),
    ("доля", "доля адресата в праве на гараж («1/2»)", "Гараж",
     lambda c: _share(c.ownership.share) if c.ownership is not None else ""),
    ("самопотребление_счётчика", "собственное потребление счётчика: «2 Вт — 1,46 кВт·ч в месяц (…в год)»",
     "Электроэнергия", _self_consumption),
    ("именуемый", "«именуемый» / «именуемая» по отчеству адресата — для договоров", "Адресат", _named),
    ("счётчик", "заводской номер актуального счётчика гаража", "Электроэнергия",
     lambda c: (m.meter_number if (m := _current_meter(c)) else "")),
    ("пломба_счётчика", "номер пломбы на счётчике", "Электроэнергия",
     lambda c: ((m.meter_seal_number or "") if (m := _current_meter(c)) else "")),
    ("показания_счётчика", "последние показания счётчика (или начальные при установке), кВт·ч",
     "Электроэнергия", _meter_reading),
    ("потери_в_сети", "потери в линии до кооператива, % (настройки «Электроэнергии»)", "Электроэнергия",
     _outside_loss),
    ("он_она", "«он» / «она» по отчеству адресата", "Для справки о владении", _pronoun),
    ("членство", "«является членом … с <дата> и » — только у действующего члена, иначе пусто",
     "Для справки о владении", _membership),
    ("владение", "«гаражом № 42, расположенным» или «1/2 доли в праве на гараж № 42, расположенный»",
     "Для справки о владении", _ownership_phrase),
    ("сведения_о_гараже", "площадь и кадастровые номера гаража и участка (какие известны)",
     "Для справки о владении", _garage_facts),
    ("нет_задолженности", "фраза «Задолженности … не имеет.» — только если долга нет, иначе пусто",
     "Для справки о владении", _no_debt),
    *_party_variables("продавец", "Продавец", lambda c: c.person),
    *_party_variables("покупатель", "Покупатель", lambda c: c.buyer),
    ("контрагент", "наименование контрагента", "Контрагент", lambda c: c.counterparty_attr("name")),
    ("ИНН_контрагента", "ИНН контрагента", "Контрагент", lambda c: c.counterparty_attr("inn")),
    ("КПП_контрагента", "КПП контрагента", "Контрагент", lambda c: c.counterparty_attr("kpp")),
    ("адрес_контрагента", "адрес контрагента", "Контрагент", lambda c: c.counterparty_attr("address")),
    ("email_контрагента", "электронная почта контрагента", "Контрагент", lambda c: c.counterparty_attr("email")),
    ("телефон_контрагента", "телефоны контрагента", "Контрагент", lambda c: c.counterparty_attr("phone")),
    ("договор_контрагента", "договор с контрагентом: «№ 15-Э от 01.02.2025»", "Контрагент", _counterparty_contract),
    ("предмет_продажи", "«гараж (бокс) № 42» или «1/2 доли в праве собственности на гараж (бокс) № 42»",
     "Купля-продажа", _sale_subject),
]
# Пустое значение у них — норма (фраза просто не нужна), не прочерк.
OPTIONAL_KEYS = {"членство", "нет_задолженности"}
# Сведения о гараже — при продаже гаража не из реестра прочерки задуманы.
_GARAGE_KEYS = {"гараж", "площадь_гаража", "кадастровый_номер", "кадастровый_номер_участка", "счётчик",
                "показания_счётчика", "предмет_продажи", "лицевой_счёт_гаража", "доля", "пломба_счётчика"}
_BY_KEY = {key.lower(): (key, fn) for key, _desc, _group, fn in VARIABLES}
_EMPTY_P_RE = re.compile(r"<p(?:\s[^>]*)?>(?:\s|&nbsp;)*</p>")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}<>]+?)\s*\}\}")


# {{сегодня+10}}, {{сегодня - 3}} — дата документа плюс/минус N дней
# (срок оплаты, явки и т.п.).
DATE_SHIFT_RE = re.compile(r"^сегодня\s*([+-])\s*(\d{1,4})$", re.IGNORECASE)


def _split_blank(raw: str) -> tuple[str, list[str] | None]:
    """«адрес_регистрации|____|______» → ключ и прочерк на случай пустого
    значения, построчно (каждый «|» — новая строка): так в шаблоне задаётся
    прочерк нужной длины, например до края колонки реквизитов."""
    key, *lines = raw.split("|")
    return key.strip(), ([line.strip() for line in lines] if lines else None)


def _resolve(raw_key: str, ctx: "Context") -> tuple[str, str] | None:
    """(ключ, значение) подстановки или None, если ключ неизвестен."""
    raw_key = _split_blank(raw_key)[0]
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
    raw_key = _split_blank(raw_key)[0]
    return bool(DATE_SHIFT_RE.match(raw_key)) or raw_key.lower() in _BY_KEY


def fill(html: str, ctx: Context, missing: set[str] | None = None, header: bool = False) -> str:
    """Подставить значения в HTML шаблона. Неизвестные ключи остаются как
    есть (видно, что опечатка), пустые значения — прочерк для заполнения
    от руки; их ключи складываются в missing. header — шапка документа:
    прочерки паспорта/адреса/телефона длиннее (адрес — в две строки),
    телефоны — по одному на строке."""
    def replace(match: re.Match) -> str:
        resolved = _resolve(match.group(1), ctx)
        if resolved is None:
            return match.group(0)
        key, value = resolved
        if header and key in HEADER_MULTILINE:
            value = HEADER_MULTILINE[key](ctx) or ""
        if not value.strip():
            if key in OPTIONAL_KEYS:
                return ""
            if missing is not None:
                missing.add(key)
            blank = _split_blank(match.group(1))[1]
            if blank:
                return str(Markup("<br>").join(escape(line) for line in blank))
            return HEADER_BLANKS.get(key, BLANK) if header else BLANK
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
            blank = _split_blank(match.group(1))[1]
            return " ".join(blank) if blank else BLANK
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
    skip = {
        "none": {"Адресат", "Гараж", "Для справки о владении", "Электроэнергия", "Купля-продажа", "Контрагент"},
        "person": {"Электроэнергия", "Купля-продажа", "Контрагент"},
    }.get(recipient, set())
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
# Вступление и выход — по Уставу на имя председателя (п. 5.2, 5.6); ФИО —
# подстановкой, бланк остаётся верным при смене председателя.
_TO_CHAIRMAN = "<p>Председателю правления {{кооператив_кратко}}</p><p>{{председатель_кратко_кому}}</p>"
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

# Полное ФИО от руки — отдельной строкой во всю ширину листа, под ней
# подсказка мелко («Водопроводчиков Владимир Владимирович» в 30
# подчёркиваний посреди строки не уместить).
_NAME_LINE = "_" * 70
_NAME_FIELD = (_NAME_LINE + "<br><span style=\"font-size: 9pt\">(фамилия, имя, отчество полностью)</span>")

_PAY_DETAILS = (
    "<p>Реквизиты для оплаты: получатель {{кооператив}}, ИНН {{ИНН}}, КПП {{КПП}}, "
    "р/с {{расчётный_счёт}} в {{банк}}, БИК {{БИК}}, к/с {{корр_счёт}}. "
    "В назначении платежа укажите номер лицевого счёта ({{лицевые_счета}}) и номер гаража.</p>"
)

# Договоры: разделы — по центру жирным, пункты — без отступа первой
# строки (номер пункта у левого края), с прочерками — по левому краю.
def _section(title: str) -> str:
    return f"<p style=\"text-align: center; text-indent: 0\"><b>{title}</b></p>"


def _clause(text: str) -> str:
    return f"<p style=\"text-indent: 0\">{text}</p>"


def _fill_clause(text: str) -> str:
    return f"<p style=\"text-indent: 0; text-align: left\">{text}</p>"


_TD = "<td style=\"border: none; width: 50%; vertical-align: top; text-align: left\">"
# Реквизиты и подписи Сторон — две колонки, подписи — отдельной строкой
# (на одном уровне при любой длине реквизитов); подпись председателя и
# М.П. бланка у договоров выключены — обе стороны подписываются здесь.
# Прочерки в колонке реквизитов — до её правого края (пустое поле
# вписывают от руки): _COL — вся строка, _COL_AFTER — после подписи поля.
_COL_WIDTH = 34
_COL = "_" * _COL_WIDTH
_COL_AFTER = {label: "_" * (_COL_WIDTH - n) for label, n in
              {"Дата рождения:": 14, "Паспорт:": 9, "Адрес регистрации:": 18, "Тел.:": 5}.items()}
_CONTRACT_PARTIES = (
    "<table><tbody><tr>"
    + _TD + "<p><b>Кооператив</b></p>"
    "<p>{{кооператив}}<br>ИНН {{ИНН}}, КПП {{КПП}}<br>ОГРН {{ОГРН}}<br>"
    "Адрес: {{юр_адрес}}<br>р/с {{расчётный_счёт}}<br>в {{банк}}<br>БИК {{БИК}}<br>к/с {{корр_счёт}}<br>"
    "Тел.: {{телефон_кооператива}}</p></td>"
    + _TD + "<p><b>{{сторона}}</b></p>"
    "<p>{{ФИО|" + _COL + "}}<br>Дата рождения: {{дата_рождения|" + _COL_AFTER["Дата рождения:"] + "}}<br>"
    "Паспорт: {{паспорт|" + _COL_AFTER["Паспорт:"] + "|" + _COL + "}}<br>"
    "Адрес регистрации: {{адрес_регистрации|" + _COL_AFTER["Адрес регистрации:"] + "|" + _COL + "}}<br>"
    "Тел.: {{телефон|" + _COL_AFTER["Тел.:"] + "}}<br>"
    "Лицевой счёт № {{лицевой_счёт_гаража}}</p></td>"
    "</tr><tr>"
    + _TD + "<p><br>Председатель правления<br><br>______________ /{{председатель_кратко}}/<br>М.П.</p></td>"
    + _TD + "<p><br><br><br>______________ /{{ФИО_кратко}}/</p></td>"
    "</tr></tbody></table>"
)

# Экземпляры: бумажный договор не «в двух экземплярах» — подписанный
# скан выкладывается в карточку гаража, его видит и сам владелец.
_CONTRACT_COPIES = _clause(
    "{n}. Скан подписанного Сторонами Договора размещается в личном кабинете {{{{сторона_кого}}}} на "
    "сайте Кооператива {{{{сайт}}}} в карточке гаража № {{{{гараж}}}}; бумажный экземпляр выдаётся по "
    "запросу {{{{сторона_кого}}}}."
)

_POWER_RULES = (
    _clause("2.2. Кооператив вправе:")
    + _clause("2.2.1. проверять прибор учёта (ИПУ) {sk}, целостность пломб, схему подключения и "
              "соответствие нагрузки разрешённой мощности;")
    + _clause("2.2.2. приостановить подачу электроэнергии на Объект на время плановых ремонтных работ и "
              "устранения аварий, а также при безучётном потреблении, повреждении пломб или превышении "
              "разрешённой мощности — до устранения нарушения.")
    + _clause("2.3. {s} обязуется:")
    + _clause("2.3.1. своевременно оплачивать электроэнергию и иные платежи по Договору (раздел 4);")
    + _clause("2.3.2. не превышать разрешённую мощность и не подключать третьих лиц без письменного "
              "согласия Кооператива;")
    + _clause("2.3.3. обеспечивать исправность и сохранность ИПУ и пломб, беспрепятственный доступ "
              "представителям Кооператива для снятия показаний и осмотра электропроводки;")
    + _clause("2.3.4. отвечать за техническое состояние электропроводки в границах своей балансовой "
              "принадлежности и соблюдать правила электро- и пожарной безопасности.")
)

# Оплата: наличными в руки, переводом на расчётный счёт с лицевым счётом
# гаража (по нему зачисляется оплата электроэнергии) или на сайте.
_PAYMENT_WAYS = (
    "{n}. Оплата производится одним из способов: наличными лично в руки председателю правления под "
    "расписку; банковским переводом на расчётный счёт Кооператива (раздел 6) с указанием своего "
    "лицевого счёта № {{{{лицевой_счёт_гаража}}}}; на сайте Кооператива {{{{сайт}}}} в личном кабинете."
)

# Сумма от руки — цифрами и прописью.
_AMOUNT = "______________ (________________________________________________) руб."

# Электроэнергия — раз в год, в единый срок уплаты взносов (как и всё
# остальное); чаще — по желанию, когда набежало много. Пени на
# электроэнергию не начисляются.
_PAY_TERMS = (
    " Срок оплаты — до {{срок_оплаты}} года, следующего за расчётным, за весь прошедший календарный "
    "год (единый срок уплаты взносов и платежей в Кооператив). Оплачивать электроэнергию можно и чаще "
    "— например, если потребление велико (более 100 кВт·ч), — по показаниям ИПУ, переданным через "
    "личный кабинет на сайте Кооператива или правлению. Пени за просрочку оплаты электроэнергии не "
    "начисляются."
)

# Договор купли-продажи: реквизиты Продавца и Покупателя — две колонки
# (как _CONTRACT_PARTIES), не из реестра — прочерки до края колонки.
def _sale_party(prefix: str, title: str) -> str:
    return (
        _TD + f"<p><b>{title}</b></p>"
        f"<p>{{{{{prefix}|" + _COL + "|" + _COL + "}}<br>"
        f"Дата рождения: {{{{{prefix}_дата_рождения|" + _COL_AFTER["Дата рождения:"] + "}}<br>"
        f"Паспорт: {{{{{prefix}_паспорт|" + _COL_AFTER["Паспорт:"] + "|" + _COL + "}}<br>"
        f"Адрес регистрации: {{{{{prefix}_адрес_регистрации|" + _COL_AFTER["Адрес регистрации:"] + "|"
        + _COL + "}}<br>"
        f"Тел.: {{{{{prefix}_телефон|" + _COL_AFTER["Тел.:"] + "}}</p></td>"
    )


_SALE_PARTIES = (
    "<table><tbody><tr>" + _sale_party("продавец", "Продавец") + _sale_party("покупатель", "Покупатель")
    + "</tr><tr>"
    + _TD + "<p><br>______________ /{{продавец_кратко|______________}}/</p></td>"
    + _TD + "<p><br>______________ /{{покупатель_кратко|______________}}/</p></td>"
    "</tr></tbody></table>"
)
# ФИО в преамбуле — не из реестра: строка под фамилию, имя, отчество.
_NAME_BLANK = "_" * 40

# Письма контрагентам — шапка справа: кому (реквизиты из «Контрагентов»),
# внизу — куда ответить. Подпись председателя и М.П. — бланка (для ЭДО —
# PDF с факсимиле).
_TO_CONTRACTOR = (
    "<p>{{контрагент}}</p><p>ИНН {{ИНН_контрагента|____________}}</p>"
    "<p>{{адрес_контрагента|" + _ADDRESS_BLANK + "}}</p><p>{{email_контрагента|}}</p>"
)
_CONTRACTOR_REPLY = "<p>Ответ просим направить по адресу электронной почты {{email}} или по телефону {{телефон_кооператива}}.</p>"

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
        "addressee_html": _TO_CHAIRMAN + (
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
            + _FILL + "Прежний собственник гаража:<br>" + _NAME_FIELD + "</p>"
            "<p>С Уставом кооператива ознакомлен(а), обязуюсь его соблюдать, выполнять решения общего "
            "собрания и правления, своевременно уплачивать членские и иные взносы.</p>"
            "<p style=\"text-indent: 0\">Приложение: копия документа, подтверждающего право на гараж.</p>"
            + _APPLICANT_SIGN.replace("{{ФИО_кратко}}", "______________________")
        ),
    },
    {
        "name": "Заявление о выходе из кооператива",
        "is_public": True,
        "for_members": True,
        "title": "ЗАЯВЛЕНИЕ",
        "recipient": "person",
        "show_number": False,
        "show_signature": False,
        "addressee_html": _TO_CHAIRMAN + _FROM_PERSON,
        "body_html": (
            _FILL + "Прошу исключить меня из членов {{кооператив_кратко}} по собственному желанию с "
            "«___» ______________ 20___ г. (не ранее чем через две недели после подачи заявления — "
            "п. 5.6 Устава) в связи с "
            "______________________________ (продажей, дарением гаража № {{гараж}} и т. п.).</p>"
            + _FILL + "Новый владелец гаража:<br>" + _NAME_FIELD + "<br>телефон: ________________________</p>"
            "<p>Обязуюсь погасить задолженность по взносам и платежам перед кооперативом, если она "
            "имеется на дату выхода.</p>"
            + _APPLICANT_SIGN
        ),
    },
    {
        "name": "Заявление на подключение к электрической сети",
        "code": POWER_APPLICATION_CODE,
        "is_public": True,
        "for_members": True,
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
        "name": "Жалоба",
        "is_public": True,
        "for_members": True,
        "title": "ЖАЛОБА",
        "recipient": "person",
        "show_number": False,
        "show_signature": False,
        "addressee_html": _TO_BOARD + _FROM_PERSON,
        "body_html": (
            _FILL + "Прошу рассмотреть жалобу на ______________________________ (чьи действия или "
            "бездействие, что именно).</p>"
            + _FILL + "Обстоятельства: ______________________________ (что произошло, когда, где).</p>"
            + _FILL + "Прошу: ______________________________.</p>"
            "<p>Ответ прошу дать в письменной форме.</p>"
            + _APPLICANT_SIGN
        ),
    },
    {
        "name": "Обращение",
        "is_public": True,
        "for_members": True,
        "title": "ОБРАЩЕНИЕ",
        "recipient": "person",
        "show_number": False,
        "show_signature": False,
        "addressee_html": _TO_BOARD + _FROM_PERSON,
        "body_html": (
            _FILL + "Обращаюсь к правлению по вопросу: ______________________________.</p>"
            + _FILL + "Предложение (просьба): ______________________________.</p>"
            + _APPLICANT_SIGN
        ),
    },
    {
        "name": "Договор на электроэнергию (не член кооператива)",
        "title": "ДОГОВОР о возмещении затрат на электрическую энергию и содержание электросетевого хозяйства",
        "recipient": "garage",
        "show_signature": False,
        "body_html": (
            "<p>{{кооператив}}, именуемый в дальнейшем «Кооператив», в лице председателя правления "
            "{{председатель}}, действующего на основании Устава, с одной стороны, и {{ФИО}}, владелец "
            "гаража (бокса) № {{гараж}}, расположенного за пределами территории Кооператива, "
            "{{именуемый}} в дальнейшем «Пользователь», с другой стороны, заключили настоящий Договор "
            "о нижеследующем.</p>"
            + _section("1. Предмет Договора")
            + _fill_clause("1.1. Кооператив на время действия Договора допускает подключение гаража "
                           "№ {{гараж}} (далее — Объект) к электросетевому хозяйству Кооператива с "
                           "разрешённой максимальной мощностью 4 кВт.")
            + _clause("1.2. Пользователь оплачивает потреблённую электроэнергию, возмещает технологические "
                      "потери и уплачивает взносы на содержание, ремонт и модернизацию электросетевого "
                      "хозяйства на условиях Договора.")
            + _clause("1.3. Договор не является договором энергоснабжения (ст. 539 ГК РФ). Кооператив не "
                      "является энергосбытовой или сетевой организацией и не извлекает прибыли из "
                      "перетока электроэнергии. Пользование сетью Кооператива предоставляется "
                      "безвозмездно: платежи по Договору возмещают затраты Кооператива и не являются "
                      "платой за пользование сетью или за переток электроэнергии.")
            + _clause("1.4. Пользователь не является членом Кооператива: Договор не порождает у него прав "
                      "и обязанностей члена Кооператива (участия в управлении, голоса на общих собраниях) "
                      "и прав на общее имущество Кооператива, кроме пользования электросетью в рамках "
                      "Договора.")
            + _fill_clause("1.5. Граница балансовой принадлежности и эксплуатационной ответственности — "
                           "точка присоединения провода Пользователя к сети Кооператива: "
                           "______________________________ (опора, распределительный щит, клеммная "
                           "коробка у гаража № ____). До неё за сеть отвечает Кооператив, после — "
                           "Пользователь.")
            + _clause("1.6. Подключение Объекта временное, выполнено с согласия Кооператива и не является "
                      "технологическим присоединением к электрическим сетям сетевой организации в порядке, "
                      "установленном постановлением Правительства РФ от 27.12.2004 № 861.")
            + _section("2. Права и обязанности Сторон")
            + _clause("2.1. Кооператив обеспечивает переток электроэнергии до границы балансовой "
                      "принадлежности в пределах разрешённой мощности и сообщает Пользователю об "
                      "изменении тарифов.")
            + _POWER_RULES.format(s="Пользователь", sk="Пользователя")
            + _section("3. Учёт электроэнергии и потери")
            + _clause("3.1. Учёт ведётся по индивидуальному прибору учёта (ИПУ) № {{счётчик}}; показания "
                      "на дату подписания Договора — {{показания_счётчика}} кВт·ч.")
            + _clause("3.2. Пользователь возмещает Кооперативу технологические потери (п. 50 Правил "
                      "недискриминационного доступа к услугам по передаче электрической энергии, утв. "
                      "постановлением Правительства РФ от 27.12.2004 № 861):")
            + "<ul><li>в подводящей линии — 3% от объёма по ИПУ Пользователя за расчётный период, а при "
            "установке контрольного прибора учёта (КПУ) в начале линии — разница показаний КПУ и суммы "
            "показаний всех ИПУ на линии, распределённая пропорционально потреблению;</li>"
            "<li>в сетях до Кооператива, выставляемые гарантирующим поставщиком, — {{потери_в_сети}}% "
            "от объёма по ИПУ Пользователя (при изменении норматива поставщиком процент меняется "
            "соответственно);</li>"
            "<li>собственное потребление ИПУ Пользователя, которое сам прибор не учитывает, — "
            "{{самопотребление_счётчика}} по действующему тарифу; при учёте по КПУ это "
            "потребление уже входит в разницу показаний и отдельно не начисляется.</li></ul>"
            + _section("4. Платежи и порядок расчётов")
            + _clause("4.1. Электроэнергия и потери (п. 3.2) оплачиваются по тарифам, установленным для "
                      "гарантирующего поставщика." + _PAY_TERMS)
            + _clause("4.2. Ежегодный взнос на обслуживание и содержание электросетевого хозяйства — доля "
                      "Пользователя в расходах Кооператива на электросетевое хозяйство за год (кабель, "
                      "трубы, гофра, щиты, автоматы, работы) по утверждённой смете или по фактическим "
                      "затратам, подтверждённым платёжными документами. Доля определяется пропорционально "
                      "числу гаражей, подключённых к сети Кооператива, если общим собранием не утверждён "
                      "иной порядок. Срок оплаты — до {{срок_оплаты}} каждого года. Взнос не является "
                      "платой за электроэнергию или за пользование сетью.")
            + _clause("4.3. Пользователь участвует в целевых расходах Кооператива на ремонт и модернизацию "
                      "электросетевого хозяйства (замена подводящей линии, установка КПУ и т. п.) "
                      "пропорционально своей доле — по утверждённой смете или фактическим затратам, "
                      "подтверждённым платёжными документами, до {{срок_оплаты}} соответствующего года, если "
                      "решением общего собрания не установлен иной срок.")
            + _fill_clause("4.4. Доля Пользователя в расходах Кооператива на материалы и оборудование за "
                           "последние 3 года (Приложение № 1) — " + _AMOUNT + " Срок "
                           "оплаты — до «___» ________________.")
            + _clause(_PAYMENT_WAYS.format(n="4.5"))
            + _section("5. Заключительные положения")
            + _clause("5.1. Договор вступает в силу с момента подписания и действует бессрочно. Договор "
                      "может быть расторгнут по соглашению Сторон. Каждая из Сторон вправе отказаться от "
                      "Договора, письменно уведомив другую Сторону за 30 календарных дней (ст. 450.1 ГК "
                      "РФ); Кооператив — в том числе при неоплате платежей по Договору в срок (раздел 4). "
                      "По истечении этого срока Кооператив отключает Объект от своей сети. Прекращение "
                      "Договора не освобождает Пользователя от погашения задолженности.")
            + _clause("5.2. Приложение № 1 (перечень материалов и оборудования, приобретённых "
                      "Кооперативом для электросетевого хозяйства за последние 3 года) — неотъемлемая "
                      "часть Договора.")
            + _CONTRACT_COPIES.format(n="5.3")
            + _section("6. Реквизиты и подписи Сторон")
            + _CONTRACT_PARTIES
        ).replace("{{сторона}}", "Пользователь").replace("{{сторона_кого}}", "Пользователя"),
    },
    {
        "name": "Договор на электроэнергию (член кооператива)",
        "code": POWER_CONTRACT_CODE,
        "title": "ДОГОВОР о подключении гаража к электрической сети кооператива",
        "recipient": "garage",
        "show_signature": False,
        "body_html": (
            "<p>{{кооператив}}, именуемый в дальнейшем «Кооператив», в лице председателя правления "
            "{{председатель}}, действующего на основании Устава, с одной стороны, и член Кооператива "
            "{{ФИО}}, собственник гаража (бокса) № {{гараж}} (доля в праве {{доля}}), {{именуемый}} в "
            "дальнейшем «Член кооператива», с другой стороны, на основании заявления Члена кооператива "
            "о подключении к электрической сети заключили настоящий Договор о нижеследующем.</p>"
            + _section("1. Предмет Договора")
            + _fill_clause("1.1. Кооператив подключает гараж № {{гараж}} (далее — Объект) к "
                           "электрической сети Кооператива с разрешённой максимальной мощностью 4 кВт, "
                           "напряжение 220 В / 380 В (нужное подчеркнуть), а Член кооператива оплачивает "
                           "потреблённую электроэнергию на условиях Договора.")
            + _clause("1.2. Пользование электросетью Кооператива регулируется Уставом, решениями общего "
                      "собрания и правления и настоящим Договором. Договор не является договором "
                      "энергоснабжения (ст. 539 ГК РФ); Кооператив не извлекает прибыли из перетока "
                      "электроэнергии.")
            + _fill_clause("1.3. Граница балансовой принадлежности и эксплуатационной ответственности — "
                           "точка присоединения провода Объекта к сети Кооператива: "
                           "______________________________ (опора, распределительный щит, клеммная "
                           "коробка). До неё за сеть отвечает Кооператив, после — Член кооператива.")
            + _section("2. Подключение")
            + _clause("2.1. Член кооператива за свой счёт устанавливает прибор учёта (ИПУ), отвечающий "
                      "установленным требованиям, и вводной автомат в месте, согласованном с правлением.")
            + _fill_clause("2.2. Кооператив осматривает электропроводку Объекта, пломбирует ИПУ и вводной "
                           "автомат и подключает Объект в течение ______ рабочих дней после выполнения "
                           "п. 2.1 и уплаты взноса на подключение (если он установлен решением общего "
                           "собрания) — " + _AMOUNT)
            + _section("3. Права и обязанности Сторон")
            + _clause("3.1. Кооператив обеспечивает переток электроэнергии до границы балансовой "
                      "принадлежности в пределах разрешённой мощности и сообщает об изменении тарифа.")
            + _POWER_RULES.replace("2.2", "3.2").replace("2.3", "3.3").format(
                s="Член кооператива", sk="Члена кооператива")
            + _section("4. Учёт и оплата")
            + _clause("4.1. Учёт ведётся по ИПУ № {{счётчик}}, пломба № {{пломба_счётчика}}; показания на "
                      "дату подключения — {{показания_счётчика}} кВт·ч.")
            + _clause("4.2. Электроэнергия оплачивается по тарифу для членов Кооператива, действующему на "
                      "дату показаний ИПУ." + _PAY_TERMS)
            + _clause("4.3. Расходы на содержание и ремонт электросетевого хозяйства покрываются членскими "
                      "и целевыми взносами, установленными общим собранием; отдельной платы по Договору "
                      "за них нет.")
            + _clause(_PAYMENT_WAYS.format(n="4.4"))
            + _section("5. Заключительные положения")
            + _clause("5.1. Договор вступает в силу с момента подписания и действует, пока Член "
                      "кооператива остаётся членом Кооператива и владельцем Объекта. При выходе из "
                      "Кооператива или отчуждении гаража Договор прекращается после окончательного "
                      "расчёта; с новым владельцем заключается новый договор.")
            + _CONTRACT_COPIES.format(n="5.2")
            + _section("6. Реквизиты и подписи Сторон")
            + _CONTRACT_PARTIES
        ).replace("{{сторона}}", "Член кооператива").replace("{{сторона_кого}}", "Члена кооператива"),
    },
    {
        "name": "Договор купли-продажи гаража",
        "code": SALE_CONTRACT_CODE,
        "chairman_only": True,
        # Пустой бланк — без входа: покупатель и продавец читают условия заранее.
        "is_public": True,
        "title": "ДОГОВОР купли-продажи гаража",
        "recipient": "sale",
        # Подписи Сторон — в тексте; ниже — подпись председателя и М.П.:
        # Кооператив заверяет договор (отметка в разделе 6).
        "signer_role": "Председатель правления",
        "body_html": (
            _fill_clause("{{продавец|" + _NAME_BLANK + "}}, {{продавец_именуемый|именуемый(ая)}} в дальнейшем "
                         "«Продавец», с одной стороны, и {{покупатель|" + _NAME_BLANK + "}}, "
                         "{{покупатель_именуемый|именуемый(ая)}} в дальнейшем «Покупатель», с другой стороны, "
                         "вместе именуемые «Стороны», заключили настоящий Договор о нижеследующем.")
            + _section("1. Предмет Договора")
            + _fill_clause("1.1. Продавец продаёт, а Покупатель покупает {{предмет_продажи|гараж (бокс) № ______}} "
                           "(далее — Гараж), расположенный на территории {{кооператив}} по адресу: "
                           "{{юр_адрес}}, площадью {{площадь_гаража|______}} м², кадастровый номер "
                           "{{кадастровый_номер|______________________}}, на земельном участке с "
                           "кадастровым номером {{кадастровый_номер_участка|______________________}}.")
            + _fill_clause("1.2. Гараж принадлежит Продавцу как члену Кооператива, полностью выплатившему "
                           "паевой взнос (п. 4 ст. 218 ГК РФ), что подтверждается справкой Кооператива; иные "
                           "документы (при наличии): ______________________________ (выписка из ЕГРН, договор, "
                           "свидетельство о праве на наследство и т. п.).")
            + _clause("1.3. Продавец гарантирует, что Гараж никому не продан, не подарен, не заложен, в споре "
                      "и под арестом (запрещением) не состоит, правами третьих лиц не обременён.")
            + _clause("1.4. Вместе с Гаражом к Покупателю переходит доля Продавца в имуществе общего "
                      "пользования Кооператива (п. 5.5 Устава Кооператива).")
            + _section("2. Цена и порядок расчётов")
            + _fill_clause("2.1. Цена Гаража — " + _AMOUNT + " Цена окончательная и изменению не подлежит.")
            + _fill_clause("2.2. Расчёт производится: полностью при подписании Договора / в течение ______ "
                           "дней с даты подписания (нужное подчеркнуть) — наличными или переводом на счёт "
                           "Продавца. Получение денег Продавец подтверждает распиской или подписью в "
                           "Договоре: «Деньги в сумме ______________ руб. получил(а) "
                           "______________ /{{продавец_кратко|______________}}/».")
            + _section("3. Передача Гаража")
            + _clause("3.1. Гараж передаётся Покупателю в момент подписания Договора вместе с ключами; "
                      "Договор имеет силу передаточного акта (ст. 556 ГК РФ). Покупатель осмотрел Гараж, "
                      "его состояние ему известно, претензий к Продавцу он не имеет.")
            + _fill_clause("3.2. Показания прибора учёта электроэнергии № {{счётчик|______________}} на дату "
                           "передачи — {{показания_счётчика|______________}} кВт·ч. Электроэнергию, взносы и "
                           "платежи в Кооператив до даты передачи оплачивает Продавец, после — Покупатель.")
            + _clause("3.3. Если право Продавца на Гараж зарегистрировано в Едином государственном реестре "
                      "недвижимости, право собственности Покупателя возникает с момента государственной "
                      "регистрации перехода права (ст. 551 ГК РФ); Стороны подают документы на регистрацию "
                      "в течение 30 дней со дня подписания Договора. Расходы по регистрации несёт "
                      "Покупатель.")
            + _section("4. Кооператив")
            + _clause("4.1. Продавец уведомил правление Кооператива о продаже Гаража (п. 5.4 Устава) и "
                      "подаёт председателю правления заявление о выходе из членов Кооператива (п. 5.6 "
                      "Устава), Покупатель — заявление о приёме в члены Кооператива (п. 5.2 Устава); до "
                      "вступления Покупатель уплачивает взносы и платежи наравне с членами Кооператива в "
                      "соответствии с Уставом.")
            + _clause("4.2. Покупатель ознакомлен с Уставом и порядком пользования общим имуществом и "
                      "электросетью Кооператива.")
            + _section("5. Заключительные положения")
            + _clause("5.1. Договор вступает в силу с момента подписания. Споры Стороны решают "
                      "переговорами, при недостижении согласия — в суде по месту нахождения Гаража.")
            + _clause("5.2. Договор составлен в трёх экземплярах: по одному для Продавца, Покупателя и "
                      "Кооператива; скан экземпляра Кооператива размещается в карточке гаража на сайте "
                      "Кооператива {{сайт}}.")
            + _section("6. Реквизиты и подписи Сторон")
            + _SALE_PARTIES
            # Гараж, нажитый в браке, — общее имущество супругов (ст. 35 СК РФ).
            + "<p><br></p>"
            + _fill_clause("Супруг(а) Продавца:<br>" + _NAME_FIELD + "<br>с продажей Гаража на "
                           "условиях Договора согласен(на): ______________ /подпись/. Продавец в браке не "
                           "состоит / Гараж не является общим имуществом супругов (нужное подчеркнуть): "
                           "______________ /подпись Продавца/.")
            + "<p><br></p>"
            + _fill_clause("Договор заверен {{кооператив}}. На дату заверения задолженность Продавца "
                           "по взносам и платежам перед Кооперативом: {{задолженность|______________}} руб.")
        ),
    },
    {
        "name": "Письмо контрагенту",
        "title": "",
        "recipient": "contractor",
        "signer_role": "Председатель правления",
        "addressee_html": _TO_CONTRACTOR,
        "body_html": (
            "<p style=\"text-align: center; text-indent: 0\">Уважаемые коллеги!</p>"
            "<p>Текст письма.</p>"
            + _CONTRACTOR_REPLY
        ),
    },
    {
        # Разовая работа (кабель, труба, фонарь) у физлица на НПД
        # (422-ФЗ): кооператив не налоговый агент — ни НДФЛ, ни взносов в
        # СФР, ни отчётности по исполнителю; основание расхода — чек из
        # «Мой налог» с ИНН кооператива (ст. 14 422-ФЗ). Исполнитель
        # заводится контрагентом на своё имя (ФИО, ИНН, адрес, телефон —
        # из карточки), паспорт и банк — от руки. Раздел 3 — признаки
        # гражданско-правового, а не трудового договора.
        "name": "Договор с самозанятым на выполнение работ",
        "hint": (
            "Исполнителя заведите в «Контрагентах» на его имя с ИНН. Перед заключением договора и перед "
            "оплатой проверьте его статус на сайте ФНС "
            "(<a href=\"https://npd.nalog.ru/check-status/\" target=\"_blank\" rel=\"noopener\">npd.nalog.ru/check-status</a>) "
            "и сохраните скриншот. Зарегистрироваться самозанятым можно бесплатно за несколько минут в "
            "приложении «Мой налог», на Госуслугах или в приложении банка, в том числе пенсионеру. Налог "
            "с доходов от организаций — 6 %, его платит сам исполнитель. У кооператива нет ни НДФЛ, ни "
            "взносов в СФР, ни отчётности по нему. Оплачивайте переводом с расчётного счёта — так оплата "
            "видна в выписке и сама сверяется с расходом. Чек с ИНН кооператива "
            "прикрепите к расходу у контрагента — без чека расход не подтверждён. Нельзя заключать "
            "такой договор с тем, кто был работником кооператива в последние два года (п. 8 ч. 2 "
            "ст. 6 422-ФЗ). На работы в электроустановках, кроме замены лампы, исполнитель должен "
            "иметь допуск."
        ),
        "title": "ДОГОВОР на выполнение работ",
        "recipient": "contractor",
        "show_signature": False,
        "body_html": (
            _fill_clause("{{кооператив}}, именуемый в дальнейшем «Заказчик», в лице председателя правления "
                         "{{председатель}}, действующего на основании Устава, с одной стороны, и гражданин(ка) "
                         "{{контрагент}}, ИНН {{ИНН_контрагента|____________}}, применяющий(ая) специальный "
                         "налоговый режим «Налог на профессиональный доход» (Федеральный закон от 27.11.2018 "
                         "№ 422-ФЗ), именуемый(ая) в дальнейшем «Исполнитель», с другой стороны, заключили "
                         "настоящий Договор о нижеследующем.")
            + _section("1. Предмет Договора")
            + _fill_clause("1.1. Исполнитель обязуется выполнить по заданию Заказчика следующие работы: "
                           + "_" * 60 + "<br>" + "_" * 90 + " (далее — Работы), а Заказчик — принять и "
                           "оплатить их.")
            + _fill_clause("1.2. Место выполнения Работ — территория Заказчика: {{юр_адрес}}. Срок выполнения: "
                           "с «___» ______________ 20___ г. по «___» ______________ 20___ г.")
            + _clause("1.3. Работы выполняются из материалов Заказчика / Исполнителя (ненужное зачеркнуть). "
                      "Стоимость материалов Исполнителя входит в цену Договора.")
            + _section("2. Цена и порядок оплаты")
            + _fill_clause("2.1. Цена Работ — " + _AMOUNT + " НДС не облагается: Исполнитель применяет "
                           "налоговый режим «Налог на профессиональный доход» и уплачивает налог самостоятельно.")
            + _clause("2.2. Заказчик оплачивает Работы в течение 10 рабочих дней после их приёмки "
                      "переводом на банковский счёт Исполнителя (раздел 6).")
            + _clause("2.3. Исполнитель формирует в приложении «Мой налог» чек на полученную сумму с указанием "
                      "ИНН Заказчика и передаёт его Заказчику не позднее 9-го числа месяца, следующего за "
                      "месяцем оплаты.")
            + _clause("2.4. Исполнитель обязуется в течение 3 дней письменно уведомить Заказчика об утрате "
                      "права применять налог на профессиональный доход и возместить Заказчику налоги, "
                      "взносы, пени и штрафы, начисленные ему из-за того, что Исполнитель не уведомил его "
                      "или не передал чек.")
            + _section("3. Порядок выполнения Работ")
            + _clause("3.1. Исполнитель выполняет Работы лично, своими инструментами, сам определяет способ и "
                      "время выполнения Работ в пределах срока Договора. Он не подчиняется правилам "
                      "внутреннего распорядка Заказчика. Договор не является трудовым.")
            + _clause("3.2. Исполнитель соблюдает правила техники безопасности, электро- и пожарной "
                      "безопасности и отвечает за вред, причинённый при выполнении Работ.")
            + _clause("3.3. Результат Работ принимается Заказчиком по акту либо, если стороны не составляют "
                      "акт, — подписью председателя правления на настоящем Договоре в разделе 7. "
                      "Недостатки, обнаруженные при приёмке, Исполнитель устраняет за свой счёт.")
            + _fill_clause("3.4. Гарантийный срок на результат Работ — ____ месяцев со дня приёмки.")
            + _section("4. Ответственность и споры")
            + _clause("4.1. За неисполнение или ненадлежащее исполнение Договора стороны отвечают в "
                      "соответствии с законодательством Российской Федерации.")
            + _clause("4.2. Споры решаются переговорами, при недостижении согласия — в суде по месту "
                      "нахождения Заказчика.")
            + _section("5. Срок действия Договора")
            + _clause("5.1. Договор вступает в силу с момента подписания и действует до полного исполнения "
                      "сторонами своих обязательств.")
            + _section("6. Реквизиты и подписи сторон")
            + "<table><tbody><tr>"
            + _TD + "<p><b>Заказчик</b></p>"
            "<p>{{кооператив}}<br>ИНН {{ИНН}}, КПП {{КПП}}<br>ОГРН {{ОГРН}}<br>"
            "Адрес: {{юр_адрес}}<br>р/с {{расчётный_счёт}}<br>в {{банк}}<br>БИК {{БИК}}<br>к/с {{корр_счёт}}<br>"
            "Тел.: {{телефон_кооператива}}</p></td>"
            + _TD + "<p><b>Исполнитель</b></p>"
            "<p>{{контрагент}}<br>ИНН {{ИНН_контрагента|" + _COL_AFTER["Паспорт:"][:-1] + "}}<br>"
            "Паспорт: " + _COL_AFTER["Паспорт:"] + "<br>" + _COL + "<br>"
            "Адрес: {{адрес_контрагента|" + _COL_AFTER["Тел.:"][:-1] + "}}<br>"
            "Тел.: {{телефон_контрагента|" + _COL_AFTER["Тел.:"] + "}}<br>"
            "Счёт для оплаты: " + _COL[:-16] + "<br>" + _COL + "<br>"
            "Банк, БИК: " + _COL[:-10] + "</p></td>"
            "</tr><tr>"
            + _TD + "<p><br>Председатель правления<br><br>______________ /{{председатель_кратко}}/<br>М.П.</p></td>"
            + _TD + "<p><br><br><br>______________ /______________/</p></td>"
            "</tr></tbody></table>"
            + _section("7. Отметка о приёмке Работ")
            + _fill_clause("Работы выполнены в полном объёме и в срок, Заказчик претензий по объёму и "
                           "качеству не имеет. «___» ______________ 20___ г.")
            + "<table><tbody><tr>"
            + _TD + "<p>Председатель правления<br><br>______________ /{{председатель_кратко}}/</p></td>"
            + _TD + "<p>Исполнитель<br><br>______________ /______________/</p></td>"
            "</tr></tbody></table>"
        ),
    },
    {
        "name": "Заявление об удалении учётной записи (личного кабинета)",
        "title": "ЗАЯВЛЕНИЕ об удалении учётной записи",
        "recipient": "contractor",
        "signer_role": "Председатель правления",
        "addressee_html": _TO_CONTRACTOR,
        "body_html": (
            _FILL + "{{кооператив}} (ИНН {{ИНН}}, КПП {{КПП}}, ОГРН {{ОГРН}}) в лице председателя правления "
            "{{председатель}}, действующего на основании Устава, просит удалить учётную запись (личный "
            "кабинет) кооператива: логин (номер договора, аккаунта) ______________________, а также все "
            "связанные с ней услуги и данные, и расторгнуть договор с даты удаления.</p>"
            "<p>Кооператив подтверждает, что необходимые данные сохранены, и понимает, что удаление "
            "необратимо.</p>"
            + _FILL + "Неиспользованный остаток денежных средств на балансе учётной записи (при наличии) "
            "просим вернуть на расчётный счёт кооператива: р/с {{расчётный_счёт}} в {{банк}}, БИК {{БИК}}, "
            "к/с {{корр_счёт}}.</p>"
            + _CONTRACTOR_REPLY
        ),
    },
    {
        # Письмо «Наследникам …» на последний адрес умершего собственника
        # (адресат — он сам из реестра): заказным с уведомлением о
        # вручении; даже вернувшееся — доказательство для суда.
        "name": "Письмо наследникам умершего члена кооператива",
        "hint": (
            "Отправьте заказным письмом с уведомлением о вручении на последний адрес умершего — "
            "наследники часто живут там же или забирают почту. Копию письма и квитанцию сохраните: "
            "они пригодятся для иска к наследникам («Делопроизводство → Должники»)."
        ),
        "title": "УВЕДОМЛЕНИЕ о задолженности по гаражу",
        "recipient": "garage",
        "signer_role": "Председатель правления",
        "addressee_html": "<p>Наследникам {{ФИО_от_кого}}</p><p>{{адрес}}</p>",
        "body_html": (
            "<p style=\"text-align: center; text-indent: 0\">Уважаемые наследники!</p>"
            "<p>Правление {{кооператив_кратко}} выражает соболезнование в связи со смертью "
            "{{ФИО_от_кого}}, собственника гаража (бокса) № {{гараж}}, расположенного на территории "
            "кооператива по адресу: {{юр_адрес}}.</p>"
            "<p>Гараж принадлежит наследникам со дня открытия наследства (п. 4 ст. 1152 ГК РФ). С этого "
            "дня взносы на содержание кооператива и его общего имущества (электросеть, дороги, освещение, "
            "вывоз мусора) уплачивают наследники как собственники гаража — независимо от того, "
            "зарегистрировано ли право на гараж в ЕГРН и вступили ли они в члены кооператива. По долгам "
            "наследодателя наследники отвечают в пределах стоимости наследства (ст. 1175 ГК РФ).</p>"
            + _FILL + "По данным учёта кооператива на {{сегодня}} задолженность по гаражу составляет "
            "{{задолженность|______________}} руб., в том числе на день смерти наследодателя — "
            "______________ руб.</p>"
            "<p>Просим в течение 30 дней с даты получения письма связаться с председателем правления, "
            "погасить задолженность и подать заявление о вступлении в члены кооператива (бланк — на "
            "сайте {{сайт}} в разделе «Бланки документов») или сообщить, как вы намерены распорядиться "
            "гаражом.</p>"
            + _PAY_DETAILS +
            "<p>Если наследники не откликнутся, кооператив будет вынужден обратиться в суд с иском о "
            "взыскании задолженности, пени и судебных расходов.</p>"
            "<p>Связаться с правлением: тел. {{телефон_кооператива}}, {{email}}, почтовый адрес: "
            "{{почтовый_адрес}}.</p>"
        ),
    },
    {
        # Член кооператива умер, наследники приняли наследство, но в
        # кооператив не обращаются и взносы не платят. Контакты наследников
        # нотариус без запроса суда не даст (нотариальная тайна, ст. 5
        # Основ), но претензию кредитора принять и известить о ней
        # наследников обязан (ст. 63 Основ) — просим и то, и другое.
        # Адресат — умерший собственник гаража из реестра; нотариус — от руки.
        "name": "Письмо нотариусу о наследниках умершего члена кооператива",
        "hint": (
            "Нотариуса, который ведёт наследственное дело, и номер дела можно найти по ФИО умершего и "
            "дате смерти в <a href=\"https://notariat.ru/ru-ru/help/probate-cases/\" target=\"_blank\" "
            "rel=\"noopener\">реестре наследственных дел</a> Федеральной нотариальной палаты. Параллельно "
            "стоит отправить заказное письмо «Наследникам …» на последний адрес умершего и сфотографировать "
            "объявление на воротах гаража — это пригодится в суде."
        ),
        "title": "ПРЕТЕНЗИЯ кредитора наследодателя",
        "recipient": "garage",
        "signer_role": "Председатель правления",
        "addressee_html": (
            "<p>Нотариусу ______________________<br>" + _ADDRESS_BLANK + "<br>"
            "<span style=\"font-size: 9pt\">(нотариальный округ, ФИО нотариуса)</span></p>"
            "<p>" + _ADDRESS_BLANK + "<br>" + _ADDRESS_BLANK + "<br>"
            "<span style=\"font-size: 9pt\">(адрес нотариальной конторы)</span></p>"
            "<p>от {{кооператив_кратко}}</p><p>{{почтовый_адрес}}</p>"
            "<p>тел. {{телефон_кооператива}}</p>"
        ),
        "body_html": (
            _FILL + "Наследственное дело № ______________ к имуществу {{ФИО_от_кого|" + _NAME_BLANK + "}}, "
            "{{дата_рождения|____________}} г. р., умершего(ей) «___» ______________ 20___ г., последнее "
            "место жительства: {{адрес_регистрации|" + _ADDRESS_BLANK + "}}.</p>"
            "<p>Наследодатель был(а) членом {{кооператив}} (ИНН {{ИНН}}, ОГРН {{ОГРН}}) и собственником "
            "гаража (бокса) № {{гараж}}, расположенного на территории кооператива по адресу: {{юр_адрес}}. "
            "Кооперативу известно, что наследство принято, однако наследники в кооператив не обратились, "
            "на связь с председателем и правлением не выходят, гаражом не пользуются и взносы не "
            "уплачивают.</p>"
            + _FILL + "Задолженность по членским и иным взносам и платежам за гараж на {{сегодня}} составляет "
            "{{задолженность|______________}} руб., в том числе на день смерти наследодателя — "
            "______________ руб. (расчёт прилагается).</p>"
            + _FILL + "Уведомления о задолженности, направленные кооперативом (исх. № ______ от "
            "«___» ______________ 20___ г., № ______ от «___» ______________ 20___ г.) по последнему "
            "известному адресу, остались без ответа.</p>"
            "<p>Наследники, принявшие наследство, отвечают по долгам наследодателя в пределах стоимости "
            "перешедшего к ним имущества (ст. 1175 ГК РФ). Гараж принадлежит наследникам со дня открытия "
            "наследства (п. 4 ст. 1152 ГК РФ), и с этого дня они как собственники несут бремя его "
            "содержания (ст. 210 ГК РФ), в том числе расходы на содержание общего имущества и "
            "инфраструктуры кооператива (электросеть, дороги, освещение, вывоз мусора), которыми "
            "обеспечивается гараж. Регистрация права собственности на гараж в ЕГРН от этих платежей не "
            "освобождает.</p>"
            "<p>На основании ст. 63 Основ законодательства Российской Федерации о нотариате прошу:</p>"
            "<ol><li>принять настоящую претензию кредитора наследодателя и приобщить её к наследственному "
            "делу;</li>"
            "<li>известить наследников о претензии и сообщить им, что кооператив просит в течение 30 дней "
            "связаться с председателем правления (контакты указаны ниже), погасить задолженность и "
            "решить вопрос о вступлении в члены кооператива или о дальнейшем пользовании гаражом;</li>"
            "<li>если это допускается законом или наследники на это согласны, — сообщить кооперативу "
            "фамилии, имена, отчества наследников, принявших наследство, и их почтовые адреса "
            "(телефоны), чтобы кооператив мог сам связаться с ними, направить им требование заказным "
            "письмом с уведомлением о вручении и при необходимости обратиться в суд;</li>"
            "<li>сообщить кооперативу о принятых мерах, в том числе о дате и способе извещения "
            "наследников.</li></ol>"
            "<p>Если наследники не откликнутся, кооператив будет вынужден обратиться в суд с иском о "
            "взыскании задолженности и ходатайствовать об истребовании у нотариуса сведений о "
            "наследниках (ст. 57 ГПК РФ).</p>"
            "<p>Ответ прошу направить по почтовому адресу {{почтовый_адрес}} или по электронной почте "
            "{{email}}. Телефон председателя правления: {{телефон_кооператива}}.</p>"
            "<p style=\"text-indent: 0\">Приложения:<br>"
            "1. Расчёт задолженности по гаражу № {{гараж}}.<br>"
            "2. Копии уведомлений о задолженности и документов об их отправке.<br>"
            "3. Копия документа о членстве наследодателя в кооперативе (выписка из реестра членов).<br>"
            "4. Копии решений общего собрания об установлении размера взносов.<br>"
            "5. Копия документа, подтверждающего полномочия председателя правления.</p>"
        ),
    },
    {
        # Гараж (обычно за территорией, часто самовольный) когда-то
        # запитали от сети кооператива по устной договорённости с прежним
        # председателем, письменного договора нет; владелец продал гараж,
        # не уведомив кооператив, данные покупателя не даёт, а
        # электричеством пользуются без договора и без оплаты. Самовольное (безучётное)
        # использование электроэнергии — ст. 7.19 КоАП РФ: протокол
        # составляет полиция (ст. 28.3 КоАП), она же может установить, кто
        # пользуется гаражом. Адресат — прежний владелец из реестра;
        # отдел полиции, даты и показания — от руки.
        "name": "Заявление в полицию о самовольном подключении к электросети",
        "hint": (
            "Перед подачей составьте акт осмотра (о бездоговорном потреблении) — с двумя свидетелями, "
            "фото и видео счётчика, кабеля и места ввода в гараж; акт и фото приложите. Заявление "
            "отдайте в дежурную часть отдела полиции по месту нахождения гаража и потребуйте "
            "талон-уведомление с номером КУСП, или подайте его на сайте МВД "
            "(<a href=\"https://мвд.рф/request_main\" target=\"_blank\" rel=\"noopener\">мвд.рф/request_main</a>). "
            "Ответ должны дать в течение 30 дней. Вскрывать чужой гараж нельзя даже при полиции и понятых "
            "(ст. 330 и 167 УК РФ): отключайте на своей сети — снаружи, до ввода кабеля в гараж "
            "(у стены или в месте ответвления), с актом, фото и свидетелями, а соседний гараж, "
            "запитанный от того же кабеля, переведите на отдельное ответвление. Письменного договора нет, поэтому "
            "потребление — бездоговорное: его стоимость взыскивают с того, кто фактически пользовался "
            "электричеством, как неосновательное обогащение (ст. 1102 ГК РФ) — для этого и нужно "
            "установить нового владельца, а акт осмотра и показания счётчика — главные доказательства. "
            "Права на землю под гаражом для заявления неважны: речь в нём только об электричестве."
        ),
        "title": "ЗАЯВЛЕНИЕ о самовольном использовании электрической энергии",
        "recipient": "garage",
        "signer_role": "Председатель правления",
        "addressee_html": (
            "<p>Начальнику ______________________<br>" + _ADDRESS_BLANK + "<br>"
            "<span style=\"font-size: 9pt\">(отдел (отделение) полиции)</span></p>"
            "<p>от {{кооператив_кратко}}</p>"
            "<p>{{почтовый_адрес}}</p>"
            "<p>тел. {{телефон_кооператива}}</p>"
        ),
        "body_html": (
            "<p>{{кооператив}} (ИНН {{ИНН}}, ОГРН {{ОГРН}}, адрес: {{юр_адрес}}) владеет электрической "
            "сетью, от которой электроэнергией снабжаются гаражи кооператива и соседние гаражи, "
            "расположенные за его территорией.</p>"
            + _FILL + "Гараж (бокс) № {{гараж}} (местоположение: " + _ADDRESS_BLANK + _ADDRESS_BLANK[:16] + ") "
            "подключён к сети кооператива подземным кабелем. Подключение было сделано по устной "
            "договорённости прежнего председателя правления с владельцем гаража — {{ФИО|" + _NAME_BLANK + "}}, "
            "тел. {{телефон|______________}}; письменный договор электроснабжения не заключался. "
            "Счётчик № {{счётчик|______________}} установлен ______________________ "
            "(в гараже / снаружи).</p>"
            + _FILL + "Не позднее чем с ______________ 20___ г. оплата за электроэнергию по этому гаражу не "
            "поступает, показания счётчика не передаются. Как выяснилось, {{ФИО|" + _NAME_BLANK + "}} "
            "продал(а) гараж, не уведомив кооператив; с новым владельцем никаких договорённостей об "
            "электроснабжении нет. «___» ______________ 20___ г. по телефону "
            "{{ФИО_кратко|______________}} подтвердил(а) продажу, однако сообщить, кому продан гараж, "
            "отказался(ась). Связаться с новым владельцем кооператив не может: гаражом пользуются, "
            "на уведомления, оставленные на воротах, не отвечают.</p>"
            + _FILL + "Последние показания счётчика, известные кооперативу, — {{показания_счётчика|______}} "
            "кВт·ч. При осмотре «___» ______________ 20___ г. (акт прилагается) показания составили "
            "______________ кВт·ч, то есть без договора и без оплаты потреблено ______________ кВт·ч "
            "на сумму ______________ руб. Потребление продолжается.</p>"
            "<p>Таким образом, неустановленное лицо, пользующееся гаражом, без договора и без оплаты "
            "использует электрическую энергию из сети кооператива, причиняя кооперативу имущественный "
            "ущерб. В этих действиях усматриваются признаки административного правонарушения, "
            "предусмотренного ст. 7.19 КоАП РФ (самовольное подключение к электрическим сетям и "
            "самовольное (безучётное) использование электрической энергии).</p>"
            "<p>Прошу:</p>"
            "<ol><li>зарегистрировать настоящее заявление и провести проверку;</li>"
            "<li>установить лицо, которое пользуется гаражом № {{гараж}} и потребляет электроэнергию, — "
            "в том числе опросить прежнего владельца {{ФИО_от_кого|" + _NAME_BLANK + "}} о том, кому и "
            "когда продан гараж;</li>"
            "<li>при наличии оснований привлечь виновное лицо к ответственности по ст. 7.19 КоАП РФ, "
            "а при обнаружении признаков преступления — передать материалы для решения вопроса о "
            "возбуждении уголовного дела;</li>"
            "<li>предупредить установленное лицо о необходимости в течение 10 дней обратиться в "
            "кооператив для заключения договора и оплаты потреблённой электроэнергии;</li>"
            "<li>письменно сообщить кооперативу о результатах проверки и о лице, пользующемся гаражом, — "
            "для взыскания стоимости потреблённой электроэнергии в гражданском порядке.</li></ol>"
            "<p>Кооператив намерен отключить гараж № {{гараж}} от своей сети снаружи гаража, без доступа "
            "внутрь, и составить об этом акт.</p>"
            "<p>Ответ прошу направить по почтовому адресу {{почтовый_адрес}} или по электронной почте "
            "{{email}}. Телефон председателя правления: {{телефон_кооператива}}.</p>"
            "<p>Об ответственности за заведомо ложный донос по ст. 306 УК РФ предупреждён(а).</p>"
            "<p style=\"text-indent: 0\">Приложения:<br>"
            "1. Акт осмотра гаража № {{гараж}} и счётчика (о бездоговорном потреблении), фотографии.<br>"
            "2. Расчёт стоимости потреблённой электроэнергии.<br>"
            "3. Копии документов об оплате электроэнергии прежним владельцем гаража (при наличии).<br>"
            "4. Схема подключения гаража к сети кооператива.<br>"
            "5. Копия документа, подтверждающего полномочия председателя правления.</p>"
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
            "<ol><li>" + "_" * 45 + "</li><li>" + "_" * 45 + "</li><li>" + "_" * 45 + "</li></ol>"
            "<p style=\"text-indent: 0\">Кворум имеется. Председатель заседания — {{председатель}}, "
            "секретарь — " + "_" * 45 + ".</p>"
            "<p style=\"text-indent: 0\"><b>Повестка дня:</b></p>"
            "<ol><li>__________________</li></ol>"
            "<p style=\"text-indent: 0\"><b>1. По первому вопросу</b> слушали: __________________.<br>"
            "Решили: __________________.<br>"
            "Голосовали: «за» — ___, «против» — ___, «воздержались» — ___. Решение принято.</p>"
            "<p style=\"text-indent: 0; text-align: right\">Секретарь заседания ______________ /______________/</p>"
        ),
    },
    {
        # Символическая благодарность активным членам без бухгалтера и
        # отчётности за работников: денежный подарок по договору дарения
        # (ст. 572 ГК РФ), а не оплата труда. Подарки от организации до
        # 4000 ₽ на человека за календарный год не облагаются НДФЛ (п. 28
        # ст. 217 НК РФ), страховые взносы на выплаты по договору дарения не
        # начисляются (п. 4 ст. 420 НК РФ). Дарение юрлицом свыше 3000 ₽ —
        # только письменно (п. 2 ст. 574 ГК РФ): протокол плюс расписки.
        # Подарок не должен быть привязан к объёму работ — иначе это
        # вознаграждение за услуги, и льгота не действует.
        "name": "Решение правления о поощрении членов кооператива",
        "hint": (
            "Поощрение оформляется как денежный подарок, а не как оплата за работу. Подарки от "
            "кооператива до 4000 ₽ одному человеку за календарный год не облагаются НДФЛ "
            "(п. 28 ст. 217 НК РФ), страховые взносы на них не начисляются. В лимит входят все подарки "
            "от кооператива за год. Не пишите в протоколе «за замену кабеля», «за 8 часов работы» и "
            "не рассчитывайте сумму по объёму работ: тогда это вознаграждение за услуги, и льгота "
            "не действует. Подарок свыше 3000 ₽ от организации дарится только письменно — получатель "
            "расписывается в протоколе или пишет расписку. Расход должен укладываться в статью сметы, "
            "утверждённой общим собранием, — заложите на поощрения отдельную позицию. Подарок деньгами "
            "заведите тратой статьи «Поощрения» в «Финансах за год». Подарок в счёт взносов — кнопкой "
            "«Подарок в счёт взносов» на лицевом счёте члена: взнос погасится, расход запишется сам. "
            "Если у человека два гаража, подарок можно разложить по двум счетам, но лимит 4000 ₽ — на "
            "человека, а не на гараж; система его проверит. Дарите в счёт взносов не больше начисленного. "
            "Освобождать от взносов правление может, если это разрешает устав или решение общего собрания. "
            "Нужно ли отражать такие "
            "подарки в 6-НДФЛ, если они целиком в пределах лимита, — уточните один раз в своей ИФНС."
        ),
        "title": "ПРОТОКОЛ № ___ заседания правления о поощрении членов кооператива",
        "recipient": "none",
        "show_number": False,
        "signer_role": "Председатель правления",
        "body_html": (
            "<p style=\"text-indent: 0\">Дата проведения: {{сегодня}}.<br>"
            "Место проведения: ______________________________.</p>"
            "<p style=\"text-indent: 0\">Присутствовали члены правления:</p>"
            "<ol><li>" + "_" * 45 + "</li><li>" + "_" * 45 + "</li><li>" + "_" * 45 + "</li></ol>"
            "<p style=\"text-indent: 0\">Кворум имеется. Председатель заседания — {{председатель}}, "
            "секретарь — " + "_" * 45 + ".</p>"
            "<p style=\"text-indent: 0\"><b>Повестка дня:</b> о поощрении членов кооператива, активно "
            "участвующих в его жизни, по итогам ______________ {{год}} года.</p>"
            "<p style=\"text-indent: 0\"><b>Слушали:</b> председателя правления {{председатель}}, "
            "предложившего в знак признательности поощрить денежными подарками членов {{кооператив_кратко}}, "
            "которые в {{год}} году активно и безвозмездно участвовали в жизни кооператива и в "
            "благоустройстве его территории.</p>"
            "<p style=\"text-indent: 0\"><b>Решили:</b></p>"
            "<ol><li>Подарить в знак признательности следующим членам кооператива денежные подарки или "
            "подарки в виде освобождения от уплаты взносов в кооператив на ту же сумму (ст. 572 ГК РФ):</li></ol>"
            "<table><tbody>"
            "<tr><td><b>№</b></td><td><b>Фамилия, имя, отчество</b></td><td><b>Гараж №</b></td>"
            "<td><b>Сумма, руб.</b></td><td><b>Способ: деньгами / в счёт взносов, л/с №</b></td>"
            "<td><b>Подарок получил(а), подпись, дата</b></td></tr>"
            + "".join(f"<tr><td>{i}</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>"
                      for i in range(1, 9))
            + "</tbody></table>"
            "<ol start=\"2\"><li>Подарки вручаются безвозмездно, по договору дарения (ст. 572 ГК РФ), не "
            "являются оплатой труда или вознаграждением за выполнение работ (оказание услуг) и не "
            "связаны с их объёмом. Настоящий протокол, подписанный получателями, подтверждает "
            "письменную форму дарения.</li>"
            "<li>Сумма подарков одному члену кооператива с учётом ранее полученных от кооператива в "
            "{{год}} году, по всем его гаражам и лицевым счетам, — не более 4000 (четырёх тысяч) рублей. "
            "Подарок в счёт взносов — не более суммы взносов, начисленных этому члену кооператива.</li>"
            "<li>Выплатить подарки за счёт статьи сметы {{кооператив_кратко}} на {{год}} год "
            "«______________________________»: денежные — наличными или переводом на счёт получателя, "
            "в счёт взносов — зачётом на указанном лицевом счёте, без выплаты денег.</li></ol>"
            "<p style=\"text-indent: 0\">Голосовали: «за» — ___, «против» — ___, «воздержались» — ___. "
            "Решение принято.</p>"
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


def template_hint(template: DocumentTemplate | None) -> Markup | None:
    """Подсказка правлению к стандартному шаблону («hint» в DEFAULT_TEMPLATES,
    по названию или коду) — на странице составления, не на бланке."""
    if template is None:
        return None
    hint = next((d["hint"] for d in DEFAULT_TEMPLATES if "hint" in d and (
        d["name"] == template.name or (d.get("code") and d.get("code") == template.code))), None)
    return Markup(hint) if hint else None


def _default_template(order: int, data: dict) -> DocumentTemplate:
    data = {k: v for k, v in data.items() if k != "hint"}
    return DocumentTemplate(**{
        "show_number": True, "show_signature": True, "signer_role": "Председатель", "addressee_html": "",
        "chairman_only": False, "is_public": False, "for_members": False, "sort_order": order * 10, **data,
    })


def coded_template(code: str) -> DocumentTemplate:
    """Стандартный шаблон с меткой code (справка, договор) — создаётся из
    стандартного, если его ещё нет."""
    template = database.db_session.query(DocumentTemplate).filter_by(code=code).first()
    if template is None:
        order, data = next((i, d) for i, d in enumerate(DEFAULT_TEMPLATES) if d.get("code") == code)
        template = _default_template(order, data)
        database.db_session.add(template)
        database.db_session.flush()
    return template


def certificate_template() -> DocumentTemplate:
    """Шаблон справки о владении гаражом (по нему оформляются запросы из
    кабинета)."""
    return coded_template(CERTIFICATE_CODE)


def application_document_code(application: Application) -> str | None:
    """Какой документ правление оформляет по заявлению: справку о владении
    (CERTIFICATE_CODE) или договор о подключении к электросети
    (POWER_CONTRACT_CODE — заявление подано по шаблону-заявлению на
    подключение); None — по такому заявлению документ не оформляется."""
    if application.kind == ApplicationKind.CERTIFICATE:
        return CERTIFICATE_CODE
    if application.template is not None and application.template.code == POWER_APPLICATION_CODE:
        return POWER_CONTRACT_CODE
    return None


def member_templates() -> list[DocumentTemplate]:
    """Шаблоны, по которым член кооператива подаёт заявление в правление
    из личного кабинета (applications.create)."""
    return (
        database.db_session.query(DocumentTemplate)
        .filter(DocumentTemplate.for_members.is_(True), DocumentTemplate.is_archived.is_(False))
        .order_by(DocumentTemplate.sort_order, DocumentTemplate.name)
        .all()
    )


# Строка «дата и подпись заявителя» в конце стандартных заявлений
# (_APPLICANT_SIGN) — у заявления из кабинета своя подпись под текстом,
# вторая не нужна. Вместе с пустым абзацем-отступом перед ней.
_APPLICANT_SIGN_RE = re.compile(
    r"(?:<p>(?:<br>|\s|&nbsp;)*</p>)?<p(?:\s[^>]*)?>[^<]*«_+»[^<]*/\{\{\s*ФИО_кратко\s*\}\}/\s*</p>\s*$"
)


def member_application_text(template: DocumentTemplate, person: Person, garage: Garage,
                            today: dt.date) -> tuple[str, str, str]:
    """(заголовок, тема, текст) заявления из кабинета по шаблону: данные
    заявителя и гаража подставлены, незаполненное — прочерком (поправит
    сам заявитель на бланке). Тема — остаток названия шаблона после
    заголовка («Заявление о выходе из кооператива» → «о выходе из
    кооператива»), иначе пусто. Шапка шаблона не используется — у
    заявления своя («В правление …», реквизиты заявителя)."""
    ctx = Context(today, person, garage)
    heading = fill_text(template.title, ctx) or "Заявление"
    heading = heading[:1].upper() + heading[1:].lower()
    title = ""
    if template.name.lower().startswith(heading.lower() + " "):
        title = template.name[len(heading) + 1:].strip()
    body = fill(_APPLICANT_SIGN_RE.sub("", template.body_html or ""), ctx)
    return heading, title, body


def visible_documents(person_id: int | None = None, garage_id: int | None = None,
                      code: str | None = None) -> list[IssuedDocument]:
    """Для карточек человека/гаража и кабинета — правлению все, члену —
    адресованные ему."""
    query = database.db_session.query(IssuedDocument)
    if person_id is not None:
        query = query.filter(or_(IssuedDocument.person_id == person_id, IssuedDocument.buyer_person_id == person_id))
    if garage_id is not None:
        query = query.filter(IssuedDocument.garage_id == garage_id)
    if code is not None:
        query = query.join(DocumentTemplate).filter(DocumentTemplate.code == code)
    if not is_board():
        if g.user.person_id is None:
            return []
        query = query.filter(or_(IssuedDocument.person_id == g.user.person_id,
                                 IssuedDocument.buyer_person_id == g.user.person_id))
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
    if not (is_board() or (g.user.person_id is not None
                            and g.user.person_id in (document.person_id, document.buyer_person_id))):
        abort(403)
    return document


def _clean(raw: str | None) -> tuple[str, bool]:
    """(санитизированный HTML, слишком_длинный)."""
    raw = raw or ""
    return sanitize_html(raw), len(raw) > MAX_HTML_LENGTH


def _ownership_choices() -> list[GarageOwnership]:
    return sorted(
        database.db_session.query(GarageOwnership).all(),
        key=lambda o: (o.person.full_name.lower(), garage_sort_key(o.garage)),
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
    documents = (
        database.db_session.query(IssuedDocument)
        .order_by(IssuedDocument.issued_at.desc(), IssuedDocument.id.desc())
        .limit(500)
        .all()
    )
    return render_template(
        "documents/index.html", templates=templates, documents=documents, missing_defaults=missing_defaults,
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
            chairman_only=source.chairman_only, is_public=source.is_public, for_members=source.for_members,
        )
    else:
        draft = template or DocumentTemplate(
            name="", title="", addressee_html="", body_html="", recipient="none",
            show_number=True, show_signature=True, signer_role="Председатель", sort_order=0, chairman_only=False,
            is_public=False, for_members=False,
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
        draft.for_members = bool(f.get("for_members"))
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


def _sale_parties_from(form):
    """(продавец, гараж, покупатель) договора купли-продажи: owner=
    "<person_id>:<garage_id>" — продавец-собственник с гаражом; продавец не
    из реестра — гараж отдельно (garage_id) или тоже прочерком;
    buyer_id — покупатель. Любой может быть None — прочерки в тексте."""
    seller, garage = _recipient_from({"owner": form.get("owner")}, "garage") if form.get("owner") else (None, None)
    if seller is None:
        garage_id = form.get("garage_id", type=int)
        garage = database.db_session.get(Garage, garage_id) if garage_id else None
    buyer_id = form.get("buyer_id", type=int)
    buyer = database.db_session.get(Person, buyer_id) if buyer_id else None
    return seller, garage, buyer


def _counterparty_from(form) -> Counterparty | None:
    counterparty_id = form.get("counterparty_id", type=int)
    return database.db_session.get(Counterparty, counterparty_id) if counterparty_id else None


def _needs_recipient(recipient: str, person, garage, counterparty=None) -> bool:
    if recipient == "contractor":
        return counterparty is None
    if recipient == "sale":
        return False  # стороны не из реестра — прочерки
    return (recipient != "none" and person is None) or (recipient == "garage" and garage is None)


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    """Составить документ: по шаблону (template_id), копией другого
    документа (copy_of) или справку по заявлению о её выдаче (application_id)."""
    form = request.form if request.method == "POST" else request.args
    source = cert_application = None
    if form.get("application_id"):
        cert_application = database.db_session.get(Application, form.get("application_id", type=int))
        code = application_document_code(cert_application) if cert_application is not None else None
        if code is None or cert_application.status != ApplicationStatus.SUBMITTED:
            flash(_("Заявление не найдено или по нему уже принято решение."), "warning")
            return redirect(url_for("applications.list_applications", status="submitted"))
        template = coded_template(code)
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

    buyer = counterparty = None
    if cert_application is not None:
        person, garage = _recipient_from({"owner": f"{cert_application.person_id}:{cert_application.garage_id or 0}"}, recipient)
        if person is None and request.method == "GET":
            flash(_("Заявитель уже не числится собственником этого гаража."), "warning")
    elif source is not None and not ({"owner", "person_id", "garage_id", "buyer_id", "counterparty_id"} & set(form)):
        person, garage, buyer, counterparty = source.person, source.garage, source.buyer, source.counterparty
    elif recipient == "contractor":
        person, garage, counterparty = None, None, _counterparty_from(form)
    elif recipient == "sale":
        person, garage, buyer = _sale_parties_from(form)
    else:
        person, garage = _recipient_from(form, recipient)
    need_recipient = _needs_recipient(recipient, person, garage, counterparty)
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
            return _render_create(template, source, recipient, person, garage, coop, chairman, cert_application,
                                  title=title, addressee_html=addressee_html, body_html=body_html,
                                  issued_at=issued_at or dt.date.today(), buyer=buyer, counterparty=counterparty)
        show_number = template.show_number if template else bool(source and source.number)
        document = IssuedDocument(
            template_id=template.id if template else None,
            number=next_number(issued_at) if show_number else None,
            title=title, addressee_html=addressee_html if has_text(addressee_html) else "",
            body_html=body_html, issued_at=issued_at,
            person_id=person.id if person else None, garage_id=garage.id if garage else None,
            buyer_person_id=buyer.id if buyer else None,
            counterparty_id=counterparty.id if counterparty else None,
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
                    + (f" → {buyer.full_name}" if buyer else "")
                    + (f": {counterparty.name}" if counterparty else "")
                    + (f", гараж № {garage.number}" if garage else ""),
        )
        if cert_application is not None:
            from .applications import application_document_issued
            application_document_issued(cert_application, document)
        database.db_session.commit()
        flash(_("Документ сохранён в журнале."), "success")
        return redirect(url_for("documents.detail", document_id=document.id))

    # Дата из адреса — пересчёт текста по новой дате ({{сегодня}}, {{сегодня+N}}).
    try:
        issued_at = dt.date.fromisoformat(request.args.get("issued_at") or "")
    except ValueError:
        issued_at = None
    return _render_create(template, source, recipient, person, garage, coop, chairman, cert_application,
                          issued_at=issued_at, buyer=buyer, counterparty=counterparty)


def _render_create(template, source, recipient, person, garage, coop, chairman, cert_application=None,
                   title=None, addressee_html=None, body_html=None, issued_at=None, buyer=None, counterparty=None):
    issued_at = issued_at or dt.date.today()
    missing: set[str] = set()
    need_recipient = _needs_recipient(recipient, person, garage, counterparty)
    if not need_recipient and body_html is None:
        if source is not None and (template is None or (person == source.person and garage == source.garage
                                                         and buyer == source.buyer
                                                         and counterparty == source.counterparty)):
            title, addressee_html, body_html = source.title, source.addressee_html, source.body_html
        else:
            ctx = Context(issued_at, person, garage, buyer=buyer, counterparty=counterparty)
            title = fill_text(template.title, ctx, missing)
            addressee_html = fill(template.addressee_html, ctx, missing, header=True)
            body_html = fill(template.body_html, ctx, missing)
    if recipient == "sale":
        # Сторона не из реестра — прочерки задуманы, это не «не заполнено».
        missing = {k for k in missing
                   if not (person is None and (k.startswith("продавец") or k == "задолженность"))
                   and not (buyer is None and k.startswith("покупатель"))
                   and not (garage is None and k in _GARAGE_KEYS)}
    labels = {key: desc for key, desc, _g, _f in VARIABLES}
    debt = person_debt(person) if person is not None and not need_recipient else None
    if cert_application is not None:
        keep = {"application_id": cert_application.id}
    elif source is not None:
        keep = {"copy_of": source.id}
    else:
        keep = {"template_id": template.id}
    return render_template(
        "documents/create.html", template=template, source=source, recipient=recipient, keep=keep,
        hint=template_hint(template),
        cert_application=cert_application, person=person, garage=garage, coop=coop, chairman=chairman,
        need_recipient=need_recipient, title=title or "", addressee_html=addressee_html or "",
        body_html=body_html or "", issued_at=issued_at,
        missing=sorted(labels.get(k, k) for k in missing), debt=debt,
        show_number=template.show_number if template else bool(source and source.number),
        show_signature=template.show_signature if template else source.show_signature,
        signer_role=template.signer_role if template else source.signer_role,
        ownerships=_ownership_choices() if recipient in ("garage", "sale") and cert_application is None else [],
        persons=_person_choices() if recipient in ("person", "sale") else [],
        garages=sorted(database.db_session.query(Garage).all(), key=garage_sort_key) if recipient == "sale" else [],
        buyer=buyer, counterparty=counterparty,
        counterparties=(database.db_session.query(Counterparty).order_by(Counterparty.name).all()
                        if recipient == "contractor" else []),
        # Доля в праве на недвижимость отчуждается только у нотариуса
        # (ст. 42 Федерального закона № 218-ФЗ) — предупреждение.
        sale_share=(recipient == "sale" and person is not None and garage is not None
                    and any(o.person_id == person.id and o.share != 1 for o in garage.ownerships)),
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
    # Справка по заявлению удалена — заявление снова ждёт решения.
    from .applications import certificate_withdrawn
    certificate_withdrawn(document)
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
        "addressee_html": fill(template.addressee_html, ctx, header=True),
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
