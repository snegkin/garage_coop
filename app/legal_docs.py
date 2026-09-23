"""
Делопроизводство (`/legal-docs/`) — взыскание задолженности через суд:
уведомление о задолженности, оплата госпошлины, исковое заявление. Все три
инструмента работают на одном и том же мультивыборе должников (см.
list_debtor_persons/_debtor_picker.html) — правление выбирает людей один
раз на странице инструмента и сразу получает печатную форму на каждого.

Судебный участок для конкретного должника определяется по последнему
известному месту жительства (Person.court_section_id, правление
проставляет вручную — по адресу автоматически определить участок нельзя,
нет открытого API «адрес → участок»), а если он не заполнен — используется
участок по месту нахождения самого кооператива
(Cooperative.default_court_section_id). См. resolve_court_section().

Госпошлина (suggest_state_duty) — только справочная подсказка по таблице
ст. 333.19 НК РФ в редакции с 09.09.2024 (для исковых заявлений
имущественного характера, НЕ для заявлений о вынесении судебного приказа,
где пошлина составляет 50% от этой суммы) — сумма на печатной форме ВСЕГДА
остаётся редактируемой, правление обязано сверить её с актуальной нормой
перед оплатой.
"""
import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response
from markupsafe import Markup, escape
from sqlalchemy.orm import joinedload

from . import database
from . import audit
from . import penalty
from . import name_declension
from .i18n import translate as _
from .auth import roles_required
from .accounting import balance, reallocate_member_charges, charge_paid_amount
from .persons import build_statement
from .models import (
    Person, Cooperative, CourtSection, RoleEnum, MemberAccount, PersonalAccount,
    GarageOwnership, Charge, FeeType, KeyRate, CORE_FEE_TYPE_CODES,
)

bp = Blueprint("legal_docs", __name__, url_prefix="/legal-docs")


# ---------------------------------------------------------------------------
# Общее: список должников, судебный участок
# ---------------------------------------------------------------------------

def list_debtor_persons() -> list[dict]:
    """
    Все люди с отрицательным балансом хотя бы по одному лицевому счёту —
    взносы/налог (MemberAccount, свой) или электричество (PersonalAccount,
    через владение гаражом, как в pd4._collect_member_debts) — с суммой
    долга (без пени, для показа в пикере). Пеня и точная сумма иска
    считаются отдельно на конкретной странице инструмента, здесь только
    ориентировочный итог, чтобы было по чему сортировать/искать.

    debt_by_category — та же сумма, но разбитая по видам (ключи — см.
    debt_categories() ниже: "fee:<id>" для непеневых FeeType, "electricity"
    для платы за электричество) — используется пикером (_debtor_picker.html)
    для клиентской фильтрации по чек-боксам «учитывать в подборке»: за
    электричество кооператив в суд не обращается и претензий не рассылает
    (должника просто отключают до погашения, см. app/power.py), поэтому
    оно должно быть исключаемым из выбора отдельно от остальных видов
    долга. "debt" — сумма ВСЕХ категорий сразу (как и раньше, для сортировки
    и как безопасный дефолт там, где фильтр ещё не применяется).

    oldest_unpaid_year_by_category — самый старый год начисления (Charge.year),
    которое до сих пор не погашено полностью (charge.amount - charge_paid_amount(charge) > 0),
    по каждой из тех же категорий — используется фильтром «непогашенные
    начисления не менее N лет» в пикере (актуально из-за трёхлетнего срока
    исковой давности — чем старее непогашенное начисление, тем горячее
    вопрос успеть подать в суд). Категория отсутствует в словаре, если по
    ней все начисления полностью погашены (текущий долг образовался только
    из недавних, ещё не просроченных так сильно начислений — такое
    возможно, если частичные оплаты по FIFO закрыли самые старые целиком).
    """
    debt_by_category: dict[int, dict[str, Decimal]] = {}
    oldest_unpaid_year_by_category: dict[int, dict[str, int]] = {}

    def _note_unpaid_years(person_id, key, charges):
        for c in charges:
            if c.amount - charge_paid_amount(c) > Decimal("0.004"):
                years = oldest_unpaid_year_by_category.setdefault(person_id, {})
                years[key] = min(years.get(key, c.year), c.year)

    member_accounts = (
        database.db_session.query(MemberAccount)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(FeeType.is_penalty.is_(False))
        .all()
    )
    for ma in member_accounts:
        b = balance(ma)
        if b < 0:
            bucket = debt_by_category.setdefault(ma.person_id, {})
            key = f"fee:{ma.fee_type_id}"
            bucket[key] = bucket.get(key, Decimal("0")) - b
            _note_unpaid_years(ma.person_id, key, ma.charges)

    ownerships = database.db_session.query(GarageOwnership).all()
    for o in ownerships:
        garage = o.garage
        if garage.account is not None:
            b = balance(garage)
            if b < 0:
                # Электричество — общий счёт на гараж, не персональный:
                # при нескольких собственниках относим весь долг на каждого
                # (тот же принцип, что и на карточке личного кабинета) —
                # пикер и печатная форма всё равно строятся по человеку.
                bucket = debt_by_category.setdefault(o.person_id, {})
                bucket["electricity"] = bucket.get("electricity", Decimal("0")) - b
                _note_unpaid_years(o.person_id, "electricity", garage.charges)

    if not debt_by_category:
        return []

    persons = database.db_session.query(Person).filter(Person.id.in_(debt_by_category.keys())).order_by(Person.full_name).all()
    return [
        {
            "person": p, "debt_by_category": debt_by_category[p.id],
            "debt": sum(debt_by_category[p.id].values(), Decimal("0")),
            "oldest_unpaid_year_by_category": oldest_unpaid_year_by_category.get(p.id, {}),
        }
        for p in persons
    ]


def debt_categories() -> list[dict]:
    """Виды задолженности для чек-боксов «учитывать в подборке» во всех
    пикерах должников (уведомление о задолженности, госпошлина, иск,
    заказные письма — app/postal_letters.py). key — стабильный
    идентификатор, тот же, что и в debt_by_category выше и в
    parse_selected_categories(). default — по CORE_FEE_TYPE_CODES
    (models.py): за электричество кооператив не судится и не рассылает
    претензии (отключает должника, см. app/power.py), прочие
    нестандартные виды (напр. "telecom_disputes" — взысканная госпошлина,
    не первичный долг) — тоже по умолчанию не входят, их надо включать
    осознанно через чек-бокс."""
    fee_types = (
        database.db_session.query(FeeType)
        .filter(FeeType.is_penalty.is_(False))
        .order_by(FeeType.name)
        .all()
    )
    cats = [
        {"key": f"fee:{ft.id}", "label": ft.name, "default": ft.code in CORE_FEE_TYPE_CODES}
        for ft in fee_types
    ]
    cats.append({"key": "electricity", "label": _("Электричество"), "default": False})
    return cats


def default_debt_categories() -> set[str]:
    return {c["key"] for c in debt_categories() if c["default"]}


def parse_selected_categories(source) -> set[str]:
    """source — request.form/request.args (любой объект с .getlist).
    Чек-боксы, которые пришли НЕ отмеченными, браузер вообще не отправляет
    — то есть «пользователь снял все галки» и «форма фильтр не показывала
    вовсе» неотличимы на уровне HTTP-запроса в любом случае, поэтому при
    полном отсутствии поля возвращаем безопасный дефолт
    (default_debt_categories()), а не пустой набор."""
    values = source.getlist("category")
    return set(values) if values else default_debt_categories()


def resolve_court_section(person: Person, coop: Cooperative) -> CourtSection | None:
    """Участок по последнему месту жительства должника — если известен,
    иначе участок по месту нахождения кооператива."""
    return person.court_section or coop.default_court_section


def _selected_persons_or_redirect():
    person_ids = [int(x) for x in request.form.getlist("person_id")]
    if not person_ids:
        flash(_("Выберите хотя бы одного должника."), "danger")
        return None
    persons = database.db_session.query(Person).filter(Person.id.in_(person_ids)).order_by(Person.full_name).all()
    if not persons:
        abort(404)
    return persons


def _render_pdf_or_fallback(template_name: str, filename: str, fallback_template: str, **context):
    html_str = render_template(template_name, **context)
    try:
        import weasyprint
    except ImportError:
        flash(_("Для скачивания PDF нужна библиотека weasyprint. Установите: pip install weasyprint"), "danger")
        return render_template(fallback_template, **context)
    pdf_bytes = weasyprint.HTML(string=html_str, base_url=request.url_root).write_pdf()
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _coop_and_chairman():
    coop = database.db_session.query(Cooperative).first()
    chairman = database.db_session.query(Person).filter(Person.is_chairman.is_(True)).first()
    return coop, chairman


def _key_rates():
    key_rows = database.db_session.query(KeyRate).order_by(KeyRate.effective_date).all()
    return [r.effective_date for r in key_rows], [r.rate_percent for r in key_rows]


def compute_claim_totals(person: Person, coop: Cooperative, target_date: dt.date, categories: set[str]) -> dict:
    """
    Сумма основного долга (без пени, из build_statement) + официальный
    расчёт пени день-в-день (penalty.compute_charge_penalty_breakdown —
    та же функция, что и persons.penalty_calculation, готовящая расчёт
    именно для суда) — общая цена иска, используется и для подсказки
    госпошлины (тул 2), и для суммы требования в исковом заявлении (тул 3).

    categories — см. debt_categories(): по умолчанию электричество в цену
    иска не входит (кооператив за него не судится, см. build_statement) —
    председатель может включить его вручную через чек-бокс на странице
    выбора должников. Пеня считается только по счетам ВЫБРАННЫХ видов
    взноса (электричество своего вида пени не имеет вовсе).
    """
    summary = build_statement(person, categories=categories)
    key_dates, key_rates = _key_rates()

    fee_type_ids = {int(key.split(":", 1)[1]) for key in categories if key.startswith("fee:")}
    charges = (
        database.db_session.query(Charge)
        .join(MemberAccount, Charge.account_id == MemberAccount.id)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(FeeType.is_penalty.is_(False), MemberAccount.person_id == person.id, MemberAccount.fee_type_id.in_(fee_type_ids))
        .all()
    ) if fee_type_ids else []
    amnesties = penalty.load_amnesty_periods(coop)
    penalty_entries = []
    penalty_total = Decimal("0")
    for charge in charges:
        periods = penalty.compute_charge_penalty_breakdown(charge, coop, target_date, key_dates, key_rates, amnesties)
        if not periods:
            continue
        subtotal = sum((p["amount"] for p in periods), Decimal("0"))
        penalty_entries.append({"charge": charge, "account": charge.account, "periods": periods, "subtotal": subtotal})
        penalty_total += subtotal
    penalty_entries.sort(key=lambda e: (e["account"].fee_type.name, e["charge"].year))

    debt = -summary["balance_excl_penalty"] if summary["balance_excl_penalty"] < 0 else Decimal("0")
    return {
        "summary": summary, "debt": debt,
        "penalty_entries": penalty_entries, "penalty_total": penalty_total,
        "claim_amount": debt + penalty_total,
    }


# Таблица госпошлины по имущественным искам — ст. 333.19 НК РФ в редакции,
# действующей с 09.09.2024 (Федеральный закон от 08.08.2024 № 259-ФЗ).
# Только для ИСКОВОГО заявления — для заявления о вынесении судебного
# приказа пошлина составляет 50% от рассчитанной здесь суммы (не
# реализовано отдельно: раздел «Делопроизводство» формирует именно иски).
# Верхняя граница диапазона, (базовая часть, % с суммы сверх нижней границы диапазона)
_STATE_DUTY_BRACKETS = [
    (Decimal("100000"), Decimal("0"), Decimal("0"), Decimal("0")),
    (Decimal("300000"), Decimal("100000"), Decimal("4000"), Decimal("0.03")),
    (Decimal("500000"), Decimal("300000"), Decimal("10000"), Decimal("0.025")),
    (Decimal("1000000"), Decimal("500000"), Decimal("15000"), Decimal("0.02")),
    (Decimal("3000000"), Decimal("1000000"), Decimal("25000"), Decimal("0.01")),
    (Decimal("8000000"), Decimal("3000000"), Decimal("45000"), Decimal("0.007")),
    (Decimal("24000000"), Decimal("8000000"), Decimal("80000"), Decimal("0.0035")),
    (Decimal("50000000"), Decimal("24000000"), Decimal("136000"), Decimal("0.003")),
    (Decimal("100000000"), Decimal("50000000"), Decimal("214000"), Decimal("0.002")),
]
_STATE_DUTY_MAX = Decimal("900000")


def _fmt_amount(value: Decimal) -> str:
    return audit.format_amount(value)


# Приказное производство (гл. 11 ГПК РФ) для взыскания задолженности по
# обязательным платежам/взносам с членов потребительского кооператива
# прямо предусмотрено абзацем десятым ст. 122 ГПК РФ (введён/действует в
# редакции Федерального закона от 28.11.2018 № 451-ФЗ) — без судебного
# заседания, госпошлина 50% от обычной ставки (см. state_duty.
# suggest_state_duty/пп. 2 п. 1 ст. 333.19 НК РФ). Исковое — обычный
# гражданский процесс с заседанием (ст. 131-132 ГПК РФ), госпошлина в
# полном размере. Правление выбирает вид на странице выбора должников
# (см. legal_docs/_debtor_picker.html: proceeding_type) один раз на весь
# пакет — оба вида требуют разного оформления шапки/текста, это не просто
# другая цифра, как у чек-бокса на странице оплаты госпошлины.
_PROCEEDING_LABELS = {
    "claim": {
        "doc_title": "Исковое заявление",
        "doc_subtitle": "о взыскании задолженности по членским (целевым) взносам, земельному налогу, пени и судебных расходов",
        "party_label": "Ответчик",
        "form_basis": "ст. 131-132 ГПК РФ",
        "demand_intro": "Взыскать с",
        "doc_name_genitive": "искового заявления",
    },
    "writ": {
        "doc_title": "Заявление о вынесении судебного приказа",
        "doc_subtitle": "о взыскании задолженности по членским (целевым) взносам и пени",
        "party_label": "Должник",
        "form_basis": (
            "абзацем десятым ст. 122, ст. 121, 123, 124 ГПК РФ"
        ),
        "demand_intro": "Вынести судебный приказ о взыскании с",
        "doc_name_genitive": "заявления о вынесении судебного приказа",
    },
}


def build_lawsuit_header(person: Person, coop: Cooperative, court_section: CourtSection | None,
                          proceeding_type: str = "claim") -> dict:
    """
    Данные для правой колонки шапки искового заявления/заявления о
    вынесении судебного приказа (суд + должник — реквизиты истца/
    взыскателя-кооператива уже даны слева, в общем бланке-шапке
    _letterhead.html, повторять их справа незачем) — фактические данные,
    не редактируемый текст: поправить их можно только через сами карточки
    (участок должника/кооператива, адрес человека), не вручную в черновике.

    В шапке ответчик указан как «Фамилия И.О.» (person.short_name), а не
    полным ФИО — так же, как председатель указан в подписях (см.
    _macros.html: signature_block) — по просьбе правления, для краткости
    официальной формы. Полное ФИО в родительном падеже по-прежнему
    используется в тексте просительной части (build_lawsuit_body) — там
    оно необходимо для точной идентификации взыскиваемого лица.
    """
    labels = _PROCEEDING_LABELS[proceeding_type]
    court_name = court_section.name if court_section else "____________________ (судебный участок не определён)"
    court_address = court_section.court_address if court_section and court_section.court_address else "____________________"
    address = person.residence_address or person.registration_address or "адрес не известен, см. материалы дела"
    return {
        "court_name": court_name, "court_address": court_address,
        "defendant_name": person.short_name, "defendant_address": address,
        "party_label": _(labels["party_label"]),
        "doc_title": _(labels["doc_title"]), "doc_subtitle": _(labels["doc_subtitle"]),
    }


def build_lawsuit_body(person: Person, coop: Cooperative, totals: dict, duty_amount: Decimal | None,
                        today: dt.date, proceeding_type: str = "claim") -> str:
    """
    Черновик ОСНОВНОЙ, содержательной части искового заявления/заявления о
    вынесении судебного приказа — обстоятельства, правовое основание,
    расчёт, просительная часть, приложения. ЧИСТЫЙ ТЕКСТ (не HTML),
    подаётся в редактируемый <textarea>: правление обязано просмотреть и
    при необходимости поправить формулировки/суммы перед подачей в суд.
    Шапка (суд, истец, ответчик/должник) и заголовок — фактические
    данные, рисуются отдельно вокруг этого текста (см.
    build_lawsuit_header, legal_docs/_macros.html: lawsuit_doc) и в этот
    текст не входят.

    Правовое основание — 338-ФЗ «О гаражных объединениях…» от 24.07.2023:
    ст. 26 ч. 9 (право взыскания взносов и пеней в судебном порядке) и
    ст. 27 ч. 3 (обязанность вносить те же платежи распространяется и на
    собственников гаражей, НЕ являющихся членами кооператива, если их
    гараж находится в границах территории, где кооператив действует, —
    в том же порядке, что и для членов). Ответчик/должник может не быть
    членом кооператива, поэтому вступительный абзац не предполагает
    членство, а прямо указывает обе нормы — применимая к конкретному
    ответчику определяется судом по факту членства, значения для
    обязанности платить это не имеет. Далее, в зависимости от
    proceeding_type ("claim"/"writ"), либо ст. 131-132 ГПК РФ (обычное
    исковое), либо абзац десятый ст. 122 + ст. 121, 123, 124 ГПК РФ
    (приказное — доступно именно для взыскания обязательных платежей/
    взносов с членов потребительского кооператива). Госпошлина для
    приказного — 50% от суммы для искового (см. lawsuit_draft/
    lawsuit_print: duty_amount уже уполовинен на момент вызова этой
    функции, здесь просто подставляется).

    Про пеню: программа считает её не по формуле устава, а по методике
    Банка России (см. app/penalty.py) — в тексте иска это указывается
    коротко, без цитирования норм ЖК РФ по аналогии, чтобы не запутывать
    формулировку правовым основанием из другой отрасли законодательства.
    """
    labels = _PROCEEDING_LABELS[proceeding_type]
    ownerships = (
        database.db_session.query(GarageOwnership)
        .filter_by(person_id=person.id)
        .all()
    )
    if ownerships:
        garages_text = ", ".join(
            f"№{o.garage.number} (доля {o.share})" for o in ownerships
        )
    else:
        garages_text = "____________________"

    debt = totals["debt"]
    penalty_total = totals["penalty_total"]
    claim_amount = totals["claim_amount"]
    duty_text = _fmt_amount(duty_amount) if duty_amount is not None else "________ руб. (квитанция не сформирована)"
    total_claim = claim_amount + (duty_amount or Decimal("0"))

    year_from = totals["summary"].get("year_from")
    year_to = totals["summary"].get("year_to")
    period_text = (
        f"с {year_from} по {year_to} г." if year_from and year_to and year_from != year_to
        else (f"за {year_from} г." if year_from else "за период образования задолженности")
    )

    party_label = labels["party_label"]
    coop_label = coop.short_name if coop and coop.short_name else (coop.full_name if coop else "кооператива")
    defendant_genitive = name_declension.genitive(person.full_name)

    return (
        f"{party_label} является собственником гаража(ей) {garages_text}, расположенного(ых) в "
        f"границах территории кооператива {coop_label}. Обязанность вносить членские и/или "
        f"целевые взносы установлена уставом кооператива для его членов и Федеральным законом от "
        f"24.07.2023 № 338-ФЗ «О гаражных объединениях и о внесении изменений в отдельные "
        f"законодательные акты Российской Федерации» — как для членов кооператива (ч. 9 ст. 26), так "
        f"и для собственников гаражей, не являющихся его членами (ч. 3 ст. 27), в одинаковом порядке. "
        f"Несмотря на это, {party_label.lower()} допустил образование задолженности {period_text}.\n\n"
        f"В случае неуплаты взносов и пеней кооператив вправе взыскать их в судебном порядке (ч. 9 "
        f"ст. 26 Федерального закона от 24.07.2023 № 338-ФЗ). Пеня рассчитана по методике "
        f"ЦБ РФ (расчёт прилагается).\n\n"
        f"Расчёт суммы {'требования' if proceeding_type == 'writ' else 'иска'}:\n"
        f"— основной долг по взносам: {_fmt_amount(debt)};\n"
        f"— пеня за просрочку (расчёт прилагается): {_fmt_amount(penalty_total)};\n"
        f"— судебные расходы (уплаченная государственная пошлина): {duty_text}.\n"
        f"Итого ко взысканию: {_fmt_amount(total_claim)}.\n\n"
        f"На основании изложенного, руководствуясь ст. 26 Федерального закона от 24.07.2023 № 338-ФЗ, "
        f"{labels['form_basis']},\n\n"
        f"ПРОШУ:\n"
        f"{labels['demand_intro']} {defendant_genitive} в пользу {coop_label} "
        f"задолженность по взносам в размере {_fmt_amount(debt)}, пеню в размере {_fmt_amount(penalty_total)} "
        f"и судебные расходы по уплате государственной пошлины в размере {duty_text}, "
        f"а всего {_fmt_amount(total_claim)}.\n\n"
        f"Приложения:\n"
        f"1. Расчёт задолженности и пени.\n"
        f"2. Копия {labels['doc_name_genitive']} и приложений для {party_label.lower()}а.\n"
        f"3. Документ об уплате государственной пошлины.\n"
        f"4. Доказательства направления {party_label.lower()}у уведомления о задолженности.\n"
        f"5. Документы, подтверждающие членство/право собственности {party_label.lower()}а на гараж."
    )


def _paragraphs_html(text: str) -> Markup:
    """
    Превращает отредактированный правлением черновик (обычный текст,
    абзацы разделены пустой строкой) в HTML-параграфы — только так к
    каждому абзацу применяется typографский отступ красной строки (см.
    _print_style.html: p.legal-p), недостижимый чистым CSS на едином
    white-space:pre-wrap блоке. Одиночный перенос строки внутри абзаца
    (напр. между пунктами списка приложений) сохраняется как <br>, не
    начинает новый абзац с отступом. Текст экранируется до вставки HTML.
    """
    parts = []
    for para in text.split("\n\n"):
        para = para.strip("\n")
        if not para:
            continue
        parts.append(f'<p class="legal-p">{str(escape(para)).replace(chr(10), "<br>")}</p>')
    return Markup("\n".join(parts))


def suggest_state_duty(claim_amount: Decimal) -> Decimal:
    """
    Только справочная подсказка — правление обязано проверить актуальную
    ставку (ст. 333.19 НК РФ) перед оплатой, сумма на печатной форме
    ВСЕГДА остаётся редактируемой (см. state_duty_review()/state_duty_print()).
    """
    if claim_amount <= 0:
        return Decimal("4000.00")
    if claim_amount <= _STATE_DUTY_BRACKETS[0][0]:
        return Decimal("4000.00")
    for upper, lower, base, rate in _STATE_DUTY_BRACKETS[1:]:
        if claim_amount <= upper:
            return (base + (claim_amount - lower) * rate).quantize(Decimal("0.01"))
    lower = _STATE_DUTY_BRACKETS[-1][0]
    amount = Decimal("314000") + (claim_amount - lower) * Decimal("0.0015")
    return min(amount, _STATE_DUTY_MAX).quantize(Decimal("0.01"))


def suggest_lawsuit_duty(claim_amount: Decimal, proceeding_type: str) -> Decimal:
    """Госпошлина для искового заявления/судебного приказа, формируемого
    в тот же приём — приказное производство (пп. 2 п. 1 ст. 333.19 НК РФ)
    вдвое дешевле обычного искового, тот же принцип, что и в чек-боксе на
    странице оплаты госпошлины (state_duty_review.html), только выбор
    делается заранее, на странице выбора должников для иска (см.
    legal_docs/_debtor_picker.html: proceeding_type), поскольку меняет не
    только сумму, но и вид/текст всего документа."""
    full = suggest_state_duty(claim_amount)
    if proceeding_type == "writ":
        return (full / 2).quantize(Decimal("0.01"))
    return full


# ---------------------------------------------------------------------------
# Судебные участки — справочник (только председатель)
# ---------------------------------------------------------------------------

@bp.route("/court-sections")
@roles_required(RoleEnum.CHAIRMAN)
def court_sections_list():
    sections = database.db_session.query(CourtSection).order_by(CourtSection.name).all()
    coop = database.db_session.query(Cooperative).first()
    return render_template("legal_docs/court_sections.html", sections=sections, coop=coop)


def _save_court_section_from_form(section: CourtSection, f):
    section.name = f["name"].strip()
    section.court_address = f.get("court_address") or None
    section.treasury_payee = f.get("treasury_payee") or None
    section.treasury_inn = f.get("treasury_inn") or None
    section.treasury_kpp = f.get("treasury_kpp") or None
    section.treasury_bank = f.get("treasury_bank") or None
    section.treasury_account = f.get("treasury_account") or None
    section.treasury_bik = f.get("treasury_bik") or None
    section.treasury_correspondent_account = f.get("treasury_correspondent_account") or None
    section.treasury_oktmo = f.get("treasury_oktmo") or None
    section.treasury_kbk = f.get("treasury_kbk") or None
    section.comment = f.get("comment") or None


@bp.route("/court-sections/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def court_section_create():
    section = CourtSection(name="")
    _save_court_section_from_form(section, request.form)
    database.db_session.add(section)
    database.db_session.flush()
    audit.record(
        "court_section.create", entity_type="court_section", entity_id=section.id,
        summary=f"Добавлен судебный участок «{section.name}»",
    )
    database.db_session.commit()
    flash(_("Судебный участок добавлен."), "success")
    return redirect(url_for("legal_docs.court_sections_list"))


@bp.route("/court-sections/<int:section_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def court_section_edit(section_id):
    section = database.db_session.get(CourtSection, section_id)
    if section is None:
        abort(404)
    _save_court_section_from_form(section, request.form)
    audit.record(
        "court_section.edit", entity_type="court_section", entity_id=section.id,
        summary=f"Изменён судебный участок «{section.name}»",
    )
    database.db_session.commit()
    flash(_("Изменения сохранены."), "success")
    return redirect(url_for("legal_docs.court_sections_list"))


@bp.route("/court-sections/<int:section_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def court_section_delete(section_id):
    section = database.db_session.get(CourtSection, section_id)
    if section is None:
        abort(404)
    name = section.name
    database.db_session.delete(section)
    audit.record(
        "court_section.delete", entity_type="court_section", entity_id=section_id,
        summary=f"Удалён судебный участок «{name}»",
    )
    database.db_session.commit()
    flash(_("Судебный участок удалён."), "success")
    return redirect(url_for("legal_docs.court_sections_list"))


@bp.route("/court-sections/assign-default", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def court_section_assign_default():
    """Назначить участок по месту нахождения самого кооператива (фолбэк,
    когда место жительства должника неизвестно)."""
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        abort(404)
    section_id = request.form.get("section_id", type=int)
    coop.default_court_section_id = section_id or None
    audit.record(
        "cooperative.default_court_section",
        summary="Изменён судебный участок по месту нахождения кооператива (для исков)",
    )
    database.db_session.commit()
    flash(_("Судебный участок кооператива обновлён."), "success")
    return redirect(url_for("legal_docs.court_sections_list"))


@bp.route("/persons/<int:person_id>/court-section", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def person_court_section_assign(person_id):
    """Назначить судебный участок по последнему месту жительства конкретного
    должника — со страницы карточки человека (persons/detail.html)."""
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    section_id = request.form.get("court_section_id", type=int)
    person.court_section_id = section_id or None
    audit.record(
        "person.court_section", entity_type="person", entity_id=person.id,
        summary=f"Изменён судебный участок по месту жительства для {person.full_name}",
    )
    database.db_session.commit()
    flash(_("Судебный участок сохранён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


# ---------------------------------------------------------------------------
# 1. Уведомление о задолженности
# ---------------------------------------------------------------------------

def build_yearly_debt_breakdown(person: Person, categories: set[str]) -> list[dict]:
    """
    Разбивка ОСНОВНОГО долга (без пени) по годам и видам взноса — сколько
    начислено и сколько оплачено в КАЖДОМ году отдельно по КАЖДОМУ виду
    взноса, а не суммирование каждого лицевого счёта за всё время его
    существования сразу (как раньше показывало уведомление о
    задолженности). Должнику так понятнее: видно, что накопилось в
    прошлом году, а что — в этом, и за какой именно взнос, а не одну
    непрозрачную сумму за произвольный срок. Оплата относится к тому
    году, когда она СДЕЛАНА (Payment.date), а не к году начисления,
    которое она гасит (порядок разнесения FIFO может закрывать старые
    начисления новым платежом — см. accounting.reallocate_garage_charges) —
    это соответствует тому, как сам должник помнит свои платежи.

    Строки без начислений и платежей (charged=paid=0) в результат не
    попадают — незначащий год/вид взноса (напр. год, когда по данному
    счёту не было ни начисления, ни оплаты) только загромождал бы
    печатную форму.

    Пеня — санкция другой природы (не долг за взнос/услугу, а начисление
    за просрочку), в разбивку не входит и здесь не считается: её итог
    показывается отдельной строкой (см. persons.build_statement,
    используемую для суммарных цифр в том же уведомлении).

    categories — см. debt_categories()/parse_selected_categories(): только
    счета из этого набора попадают в разбивку (по умолчанию — без
    электричества, за него кооператив не судится и не рассылает
    претензии).
    """
    member_accounts = (
        database.db_session.query(MemberAccount)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(MemberAccount.person_id == person.id, FeeType.is_penalty.is_(False))
        .options(joinedload(MemberAccount.charges), joinedload(MemberAccount.payments))
        .all()
    )
    member_accounts = [ma for ma in member_accounts if f"fee:{ma.fee_type_id}" in categories]

    owned_garage_ids = [
        o.garage_id for o in
        database.db_session.query(GarageOwnership).filter_by(person_id=person.id).all()
    ]
    personal_accounts = []
    if owned_garage_ids and "electricity" in categories:
        personal_accounts = (
            database.db_session.query(PersonalAccount)
            .filter(PersonalAccount.garage_id.in_(owned_garage_ids))
            .options(joinedload(PersonalAccount.garage))
            .all()
        )

    by_year_type: dict[tuple[int, str], dict[str, Decimal]] = {}

    def _row(year: int, fee_type_name: str) -> dict:
        return by_year_type.setdefault((year, fee_type_name), {"charged": Decimal("0"), "paid": Decimal("0")})

    for ma in member_accounts:
        for c in ma.charges:
            _row(c.year, ma.fee_type.name)["charged"] += c.amount
        for p in ma.payments:
            _row(p.date.year, ma.fee_type.name)["paid"] += p.amount
    electricity_name = _("Электричество")
    for pa in personal_accounts:
        for c in pa.garage.charges:
            _row(c.year, electricity_name)["charged"] += c.amount
        for p in pa.garage.payments:
            _row(p.date.year, electricity_name)["paid"] += p.amount

    return [
        {"year": year, "fee_type": fee_type_name, "charged": v["charged"], "paid": v["paid"], "balance": v["paid"] - v["charged"]}
        for (year, fee_type_name), v in sorted(by_year_type.items())
        if v["charged"] or v["paid"]
    ]


# ---------------------------------------------------------------------------
# Единая страница выбора должников — один мультивыбор (_debtor_picker.html)
# и кнопки всех инструментов под ним (уведомление, заказное письмо,
# госпошлина, иск — каждая через свой formaction). Раньше у каждого
# инструмента была своя страница с тем же самым пикером; их GET-адреса
# остались редиректами сюда, POST-обработчики не изменились.
# ---------------------------------------------------------------------------

@bp.route("/debtors")
@roles_required(RoleEnum.BOARD)
def debtors():
    return render_template(
        "legal_docs/debtors.html", debtors=list_debtor_persons(), categories=debt_categories(),
    )


@bp.route("/debt-notice", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def debt_notice():
    if request.method == "GET":
        return redirect(url_for("legal_docs.debtors"))

    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.debtors"))

    selected = parse_selected_categories(request.form)
    coop, chairman = _coop_and_chairman()
    docs = [
        {"person": p, "summary": build_statement(p, categories=selected), "years": build_yearly_debt_breakdown(p, selected)}
        for p in persons
    ]

    context = dict(
        docs=docs, coop=coop, chairman=chairman, today=dt.date.today(), hide_chat_widgets=True,
        selected_categories=selected,
    )
    if request.form.get("format") == "pdf":
        return _render_pdf_or_fallback(
            "legal_docs/debt_notice_pdf.html", "uvedomlenie_o_zadolzhennosti.pdf",
            "legal_docs/debt_notice_print.html", **context,
        )
    return render_template("legal_docs/debt_notice_print.html", **context)


# ---------------------------------------------------------------------------
# 2. Оплата госпошлины
# ---------------------------------------------------------------------------

@bp.route("/state-duty")
@roles_required(RoleEnum.BOARD)
def state_duty():
    return redirect(url_for("legal_docs.debtors"))


@bp.route("/state-duty/review", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def state_duty_review():
    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.debtors"))

    selected = parse_selected_categories(request.form)
    coop, _chairman = _coop_and_chairman()
    target_date = dt.date.today()
    items = []
    for p in persons:
        totals = compute_claim_totals(p, coop, target_date, selected)
        items.append({
            "person": p, "section": resolve_court_section(p, coop) if coop else None,
            "claim_amount": totals["claim_amount"],
            "suggested_duty": suggest_state_duty(totals["claim_amount"]),
        })
    return render_template("legal_docs/state_duty_review.html", items=items)


@bp.route("/state-duty/print", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def state_duty_print():
    person_ids = [int(x) for x in request.form.getlist("person_id")]
    if not person_ids:
        flash(_("Выберите хотя бы одного должника."), "danger")
        return redirect(url_for("legal_docs.debtors"))

    coop, _chairman = _coop_and_chairman()
    persons = database.db_session.query(Person).filter(Person.id.in_(person_ids)).order_by(Person.full_name).all()

    items = []
    for p in persons:
        raw_amount = request.form.get(f"duty_amount_{p.id}", "").strip().replace(",", ".")
        try:
            duty_amount = Decimal(raw_amount) if raw_amount else Decimal("0")
        except Exception:
            duty_amount = Decimal("0")
        items.append({
            "person": p, "section": resolve_court_section(p, coop) if coop else None,
            "duty_amount": duty_amount,
        })
        audit.record(
            "legal.state_duty_printed", entity_type="person", entity_id=p.id,
            summary=f"Сформирована квитанция на госпошлину по иску к {p.full_name} на сумму {audit.format_amount(duty_amount)}",
        )
    database.db_session.commit()

    context = dict(items=items, coop=coop, today=dt.date.today(), hide_chat_widgets=True)
    if request.form.get("format") == "pdf":
        return _render_pdf_or_fallback(
            "legal_docs/state_duty_pdf.html", "gosposhlina.pdf",
            "legal_docs/state_duty_print.html", **context,
        )
    return render_template("legal_docs/state_duty_print.html", **context)


@bp.route("/state-duty/charge", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def state_duty_charge():
    """
    Начисляет уплаченную кооперативом госпошлину на личный счёт каждого
    должника (вид взноса "telecom_disputes", FeeType.per_garage=False —
    один счёт на человека, см. её докстринг) — отдельной кнопкой на самой
    квитанции (state_duty_print.html), вручную, ПОСЛЕ того как госпошлина
    реально уплачена: сам факт печати квитанции (state_duty_print) ещё не
    значит, что деньги ушли — оплата происходит вне системы (банк/касса
    суда), автоматической привязки к реальному платежу нет.

    Суммы — те же hidden-поля duty_amount_{person_id}, что и в форме
    «Скачать PDF» на той же странице (см. state_duty_print.html) — то, что
    председатель в итоге напечатал и по факту оплатил, а не пересчитанная
    заново подсказка (сумма госпошлины могла быть скорректирована вручную
    перед печатью, см. suggest_state_duty).
    """
    person_ids = request.form.getlist("person_id")
    fee_type = database.db_session.query(FeeType).filter_by(code="telecom_disputes").first()
    if fee_type is None:
        flash(_("Вид взноса «Телекоммуникационные услуги и споры» не найден — обратитесь к разработчику."), "danger")
        return redirect(url_for("legal_docs.debtors"))

    charged = 0
    for person_id_raw in person_ids:
        person_id = int(person_id_raw)
        raw_amount = request.form.get(f"duty_amount_{person_id}", "").strip().replace(",", ".")
        try:
            amount = Decimal(raw_amount) if raw_amount else Decimal("0")
        except Exception:
            amount = Decimal("0")
        if amount <= 0:
            continue
        account = (
            database.db_session.query(MemberAccount)
            .filter_by(person_id=person_id, fee_type_id=fee_type.id, garage_id=None, is_archived=False)
            .first()
        )
        if account is None:
            continue  # не должно случаться в норме — счёт заводится автоматически всем текущим членам (garages.add_owner)
        person = database.db_session.get(Person, person_id)
        database.db_session.add(Charge(
            account_id=account.id, year=dt.date.today().year, amount=amount,
            comment=_("Госпошлина по иску, уплаченная кооперативом"),
        ))
        database.db_session.flush()
        reallocate_member_charges(account)
        audit.record(
            "legal.state_duty_charged", entity_type="member_account", entity_id=account.id,
            summary=f"Начислена госпошлина {audit.format_amount(amount)} на счёт {account.account_number} "
                    f"({person.short_name if person else person_id})",
        )
        charged += 1
    database.db_session.commit()
    if charged:
        flash(_("Госпошлина начислена на {n} счёт(ов).", n=charged), "success")
    else:
        flash(_("Нечего начислять — суммы не указаны, либо у должников не найден личный счёт."), "warning")
    return redirect(url_for("legal_docs.debtors"))


# ---------------------------------------------------------------------------
# 3. Исковое заявление
# ---------------------------------------------------------------------------

@bp.route("/lawsuit")
@roles_required(RoleEnum.BOARD)
def lawsuit():
    return redirect(url_for("legal_docs.debtors"))


@bp.route("/lawsuit/draft", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def lawsuit_draft():
    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.debtors"))

    selected = parse_selected_categories(request.form)
    proceeding_type = "writ" if request.form.get("proceeding_type") == "writ" else "claim"
    coop, _chairman = _coop_and_chairman()
    target_date = dt.date.today()
    drafts = []
    for p in persons:
        totals = compute_claim_totals(p, coop, target_date, selected)
        section = resolve_court_section(p, coop) if coop else None
        duty_amount = suggest_lawsuit_duty(totals["claim_amount"], proceeding_type)
        header = build_lawsuit_header(p, coop, section, proceeding_type)
        text = build_lawsuit_body(p, coop, totals, duty_amount, target_date, proceeding_type)
        drafts.append({"person": p, "header": header, "text": text})

    return render_template(
        "legal_docs/lawsuit_draft.html", drafts=drafts, coop=coop, proceeding_type=proceeding_type,
        selected_categories=selected,
    )


@bp.route("/lawsuit/print", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def lawsuit_print():
    person_ids = [int(x) for x in request.form.getlist("person_id")]
    if not person_ids:
        flash(_("Выберите хотя бы одного должника."), "danger")
        return redirect(url_for("legal_docs.debtors"))

    selected = parse_selected_categories(request.form)
    proceeding_type = "writ" if request.form.get("proceeding_type") == "writ" else "claim"
    coop, chairman = _coop_and_chairman()
    target_date = dt.date.today()
    persons = database.db_session.query(Person).filter(Person.id.in_(person_ids)).order_by(Person.full_name).all()
    docs = []
    for p in persons:
        text = request.form.get(f"text_{p.id}", "")
        section = resolve_court_section(p, coop) if coop else None
        header = build_lawsuit_header(p, coop, section, proceeding_type)
        # Расчёт пени прикладывается к иску отдельным приложением, если она
        # начислена — тот же официальный расчёт день-в-день, что уже
        # использовался при формировании черновика (см. build_lawsuit_body),
        # пересчитан заново на сегодня (см. compute_claim_totals), а не
        # перенесён из момента составления черновика: правление могло
        # сформировать черновик раньше, чем распечатало готовый иск.
        totals = compute_claim_totals(p, coop, target_date, selected)
        docs.append({
            "person": p, "header": header, "text": text, "body_html": _paragraphs_html(text),
            "penalty_entries": totals["penalty_entries"], "penalty_total": totals["penalty_total"],
        })

    context = dict(
        docs=docs, coop=coop, chairman=chairman, target_date=target_date,
        proceeding_type=proceeding_type, hide_chat_widgets=True,
        selected_categories=selected,
    )
    if request.form.get("format") == "pdf":
        return _render_pdf_or_fallback(
            "legal_docs/lawsuit_pdf.html", "iskovoe_zayavlenie.pdf",
            "legal_docs/lawsuit_print.html", **context,
        )
    return render_template("legal_docs/lawsuit_print.html", **context)
