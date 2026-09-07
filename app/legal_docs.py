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

from . import database
from . import audit
from . import penalty
from .i18n import translate as _
from .auth import roles_required
from .accounting import balance
from .persons import build_statement
from .models import (
    Person, Cooperative, CourtSection, RoleEnum, MemberAccount,
    GarageOwnership, Charge, FeeType, KeyRate,
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
    """
    debts: dict[int, Decimal] = {}

    member_accounts = (
        database.db_session.query(MemberAccount)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(FeeType.is_penalty.is_(False))
        .all()
    )
    for ma in member_accounts:
        b = balance(ma)
        if b < 0:
            debts[ma.person_id] = debts.get(ma.person_id, Decimal("0")) + b

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
                debts[o.person_id] = debts.get(o.person_id, Decimal("0")) + b

    if not debts:
        return []

    persons = database.db_session.query(Person).filter(Person.id.in_(debts.keys())).order_by(Person.full_name).all()
    return [{"person": p, "debt": -debts[p.id]} for p in persons]


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


def compute_claim_totals(person: Person, coop: Cooperative, target_date: dt.date) -> dict:
    """
    Сумма основного долга (без пени, из build_statement) + официальный
    расчёт пени день-в-день (penalty.compute_charge_penalty_breakdown —
    та же функция, что и persons.penalty_calculation, готовящая расчёт
    именно для суда) — общая цена иска, используется и для подсказки
    госпошлины (тул 2), и для суммы требования в исковом заявлении (тул 3).
    """
    summary = build_statement(person)
    key_dates, key_rates = _key_rates()

    charges = (
        database.db_session.query(Charge)
        .join(MemberAccount, Charge.account_id == MemberAccount.id)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(FeeType.is_penalty.is_(False), MemberAccount.person_id == person.id)
        .all()
    )
    penalty_entries = []
    penalty_total = Decimal("0")
    for charge in charges:
        periods = penalty.compute_charge_penalty_breakdown(charge, coop, target_date, key_dates, key_rates)
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


def build_lawsuit_draft(person: Person, coop: Cooperative, court_section: CourtSection | None,
                         totals: dict, duty_amount: Decimal | None, today: dt.date) -> str:
    """
    Черновик искового заявления — ЧИСТЫЙ ТЕКСТ (не HTML), подаётся в
    редактируемый <textarea>: правление обязано просмотреть и при
    необходимости поправить формулировки/суммы перед подачей в суд, это
    не готовый к печати официальный бланк, как уведомление о
    задолженности. Правовое основание — 338-ФЗ «О гаражных объединениях…»
    от 24.07.2023 ст. 26 ч. 9 (право взыскания взносов и пеней в судебном
    порядке; сам порядок/размер пени — по уставу кооператива, см.
    app/penalty.py) и ГПК РФ ст. 131-132 (форма и приложения искового
    заявления). Сумма пени — тот же официальный расчёт день-в-день, что
    уже готовится отдельным приложением к выписке для суда (см.
    persons.penalty_calculation), госпошлина — фигурирует как судебные
    расходы, взыскиваемые с ответчика, той же суммой, что была уплачена
    по квитанции (см. state_duty_print) — если квитанция ещё не
    сформирована для этого человека, оставляем сумму пустой для ручного
    заполнения.
    """
    court_name = court_section.name if court_section else "____________________ (судебный участок не определён)"
    court_address = court_section.court_address if court_section and court_section.court_address else "____________________"

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

    address = person.residence_address or person.registration_address or "адрес не известен, см. материалы дела"

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

    return (
        f"В {court_name}\n"
        f"Адрес суда: {court_address}\n\n"
        f"Истец: {coop.full_name if coop else '____________________'}"
        f"{f', ИНН {coop.inn}' if coop else ''}{f', ОГРН {coop.ogrn}' if coop and coop.ogrn else ''}\n"
        f"Адрес: {(coop.legal_address or coop.postal_address) if coop else '____________________'}\n"
        f"{f'Email: {coop.email}' if coop and coop.email else ''}\n\n"
        f"Ответчик: {person.full_name}\n"
        f"Адрес: {address}\n\n"
        f"ИСКОВОЕ ЗАЯВЛЕНИЕ\n"
        f"о взыскании задолженности по членским (целевым) взносам, пени и судебных расходов\n\n"
        f"Ответчик является членом кооператива и собственником гаража(ей) {garages_text}. "
        f"В соответствии с уставом кооператива ответчик обязан своевременно вносить членские "
        f"и/или целевые взносы, однако допустил образование задолженности {period_text}.\n\n"
        f"Согласно ч. 9 ст. 26 Федерального закона от 24.07.2023 № 338-ФЗ «О гаражных объединениях "
        f"и о внесении изменений в отдельные законодательные акты Российской Федерации» в случае "
        f"неуплаты взносов и пеней кооператив вправе взыскать их с члена кооператива в судебном "
        f"порядке. Размер и порядок начисления пени установлены уставом кооператива.\n\n"
        f"Расчёт суммы иска:\n"
        f"— основной долг по взносам: {_fmt_amount(debt)};\n"
        f"— пеня за просрочку (расчёт прилагается): {_fmt_amount(penalty_total)};\n"
        f"— судебные расходы (уплаченная государственная пошлина): {duty_text}.\n"
        f"Итого ко взысканию: {_fmt_amount(total_claim)}.\n\n"
        f"На основании изложенного, руководствуясь ст. 26 Федерального закона от 24.07.2023 № 338-ФЗ, "
        f"ст. 131-132 Гражданского процессуального кодекса Российской Федерации,\n\n"
        f"ПРОШУ:\n"
        f"Взыскать с {person.full_name} в пользу {coop.short_name if coop and coop.short_name else (coop.full_name if coop else 'кооператива')} "
        f"задолженность по взносам в размере {_fmt_amount(debt)}, пеню в размере {_fmt_amount(penalty_total)} "
        f"и судебные расходы по уплате государственной пошлины в размере {duty_text}, "
        f"а всего {_fmt_amount(total_claim)}.\n\n"
        f"Приложения:\n"
        f"1. Расчёт задолженности и пени.\n"
        f"2. Копия искового заявления и приложений для ответчика.\n"
        f"3. Документ об уплате государственной пошлины.\n"
        f"4. Доказательства направления ответчику уведомления о задолженности.\n"
        f"5. Документы, подтверждающие членство/право собственности ответчика на гараж.\n\n"
        f"«____» ____________ {today.year} г.        Председатель ____________________\n"
    )


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

@bp.route("/debt-notice", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def debt_notice():
    if request.method == "GET":
        debtors = list_debtor_persons()
        return render_template("legal_docs/debt_notice_picker.html", debtors=debtors)

    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.debt_notice"))

    coop, chairman = _coop_and_chairman()
    docs = [{"person": p, "summary": build_statement(p)} for p in persons]

    context = dict(docs=docs, coop=coop, chairman=chairman, today=dt.date.today(), hide_chat_widgets=True)
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
    debtors = list_debtor_persons()
    return render_template("legal_docs/state_duty_picker.html", debtors=debtors)


@bp.route("/state-duty/review", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def state_duty_review():
    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.state_duty"))

    coop, _chairman = _coop_and_chairman()
    target_date = dt.date.today()
    items = []
    for p in persons:
        totals = compute_claim_totals(p, coop, target_date)
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
        return redirect(url_for("legal_docs.state_duty"))

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


# ---------------------------------------------------------------------------
# 3. Исковое заявление
# ---------------------------------------------------------------------------

@bp.route("/lawsuit")
@roles_required(RoleEnum.BOARD)
def lawsuit():
    debtors = list_debtor_persons()
    return render_template("legal_docs/lawsuit_picker.html", debtors=debtors)


@bp.route("/lawsuit/draft", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def lawsuit_draft():
    persons = _selected_persons_or_redirect()
    if persons is None:
        return redirect(url_for("legal_docs.lawsuit"))

    coop, _chairman = _coop_and_chairman()
    target_date = dt.date.today()
    drafts = []
    for p in persons:
        totals = compute_claim_totals(p, coop, target_date)
        section = resolve_court_section(p, coop) if coop else None
        duty_amount = suggest_state_duty(totals["claim_amount"])
        text = build_lawsuit_draft(p, coop, section, totals, duty_amount, target_date)
        drafts.append({"person": p, "text": text})

    return render_template("legal_docs/lawsuit_draft.html", drafts=drafts)


@bp.route("/lawsuit/print", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def lawsuit_print():
    person_ids = [int(x) for x in request.form.getlist("person_id")]
    if not person_ids:
        flash(_("Выберите хотя бы одного должника."), "danger")
        return redirect(url_for("legal_docs.lawsuit"))

    coop, _chairman = _coop_and_chairman()
    target_date = dt.date.today()
    persons = database.db_session.query(Person).filter(Person.id.in_(person_ids)).order_by(Person.full_name).all()
    docs = []
    for p in persons:
        text = request.form.get(f"text_{p.id}", "")
        # Расчёт пени прикладывается к иску отдельным приложением, если она
        # начислена — тот же официальный расчёт день-в-день, что уже
        # использовался при формировании черновика (см. build_lawsuit_draft),
        # пересчитан заново на сегодня (см. compute_claim_totals), а не
        # перенесён из момента составления черновика: правление могло
        # сформировать черновик раньше, чем распечатало готовый иск.
        totals = compute_claim_totals(p, coop, target_date)
        docs.append({
            "person": p, "text": text,
            "penalty_entries": totals["penalty_entries"], "penalty_total": totals["penalty_total"],
        })

    context = dict(docs=docs, coop=coop, target_date=target_date, hide_chat_widgets=True)
    if request.form.get("format") == "pdf":
        return _render_pdf_or_fallback(
            "legal_docs/lawsuit_pdf.html", "iskovoe_zayavlenie.pdf",
            "legal_docs/lawsuit_print.html", **context,
        )
    return render_template("legal_docs/lawsuit_print.html", **context)
