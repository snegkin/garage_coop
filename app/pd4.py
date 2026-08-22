import base64
import io
from decimal import Decimal

import segno
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, Response

from . import database
from .i18n import translate as _
from .auth import login_required
from .permissions import is_board, is_privileged
from .models import MemberAccount, Person, PD4Document, Cooperative, Garage, GarageOwnership, PersonalAccount, FeeType
from .accounting import (
    balance, penalty_sibling_account, get_primary_bank_account, pd4_qr_payload, pd4_qr_payload_electricity,
)

bp = Blueprint("pd4", __name__, url_prefix="/pd4")


def _qr_data_uri(payload: str) -> str:
    """PNG QR-кода в виде self-contained base64 data URI для <img src="...">."""
    qr = segno.make(payload, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=2, border=1, dark="black", light="white")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"

@bp.route("/")
@login_required
def select():
    """
    Полный список счетов с поиском (только правление/председатель).
    Только взносы/налоги/пеня (MemberAccount) — правление печатает платёжки
    только по ним; печать по электричеству доступна лишь самому владельцу
    гаража (см. _print_electricity_slip), в этот список не попадает и
    здесь не выбирается, даже если правление знает garage_id напрямую.
    """
    if not is_board():
        return redirect(url_for("pd4.print_slips"))

    privileged = is_privileged()
    q = request.args.get("q", "").strip()
    query = database.db_session.query(MemberAccount)

    all_persons = []
    if q:
        all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
        query = query.join(Person).join(Garage).filter(
            Person.full_name.ilike(f"%{q}%") |
            Garage.number.ilike(f"%{q}%") |
            MemberAccount.account_number.ilike(f"%{q}%")
        )

    rows = []
    for account in query.all():
        if not privileged and account.fee_type.is_penalty:
            continue
        debt = balance(account)
        if debt < 0:
            rows.append((account, debt))
    rows.sort(key=lambda r: (r[0].person.full_name, r[0].garage.number))

    return render_template("pd4/select.html", rows=rows, all_persons=all_persons, q=q)


def _collect_account_ids(person_id: int) -> list[int]:
    """Собирает все ID счетов члена (взносы/налоги), у которых есть задолженность."""
    query = database.db_session.query(MemberAccount).filter(
        MemberAccount.person_id == person_id
    )
    ids = []
    for acc in query.all():
        if balance(acc) < 0:
            ids.append(acc.id)
            # авто-прицепка пени
            sibling = penalty_sibling_account(acc)
            if sibling is not None and sibling.id not in ids and balance(sibling) < 0:
                ids.append(sibling.id)
    return ids


def _collect_member_debts(person_id: int) -> tuple[list[int], list[int]]:
    """
    Все задолженности человека сразу по обоим видам платёжек — взносы
    (см. _collect_account_ids) и электричество по гаражам, которыми он
    владеет полностью или частично (для member.id используется владение
    через GarageOwnership, а не роль — рядовой член видит здесь всё своё,
    ровно как и председатель/правление видит своё личное, если сами
    владеют гаражом). Возвращает (member_account_ids, electricity_garage_ids).
    """
    member_account_ids = _collect_account_ids(person_id)
    electricity_garage_ids = []
    ownerships = database.db_session.query(GarageOwnership).filter_by(person_id=person_id).all()
    for ownership in ownerships:
        garage = ownership.garage
        if garage.account is not None and balance(garage) < 0:
            electricity_garage_ids.append(garage.id)
    return member_account_ids, list(dict.fromkeys(electricity_garage_ids))  # без дублей, если совладение странно задвоено


@bp.route("/print", methods=["GET", "POST"])
@login_required
def print_slips():
    # платёжка по электричеству конкретного гаража (кнопка «Оплатить» в /cabinet/garages) —
    # всегда только для собственника, правление сюда не имеет административного доступа
    garage_id = request.values.get("garage_id", type=int)
    if garage_id is not None:
        return _print_electricity_slip(garage_id)

    # GET — печать по конкретному счёту взносов (кнопка «Оплатить») либо по
    # всем задолженностям текущего пользователя сразу (оба вида платёжек)
    if request.method == "GET":
        account_id = request.args.get("account_id", type=int)
        if account_id is not None:
            account = database.db_session.get(MemberAccount, account_id)
            if account is None:
                abort(404)
            if not is_board() and account.person_id != g.user.person_id:
                abort(403)
            member_account_ids, electricity_garage_ids = [account_id], []
        else:
            person_id = g.user.person_id
            if person_id is None:
                flash(_("Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление."), "warning")
                return redirect(url_for("main.dashboard"))
            # все свои задолженности сразу — и взносы, и электричество по своим гаражам
            member_account_ids, electricity_garage_ids = _collect_member_debts(person_id)

        if not member_account_ids and not electricity_garage_ids:
            flash(_("Нет задолженностей — печатать нечего."), "info")
            return redirect(url_for("main.dashboard"))

        coop, bank_account, slips = _build_mixed_slips(member_account_ids, electricity_garage_ids)
        if slips is None:
            return redirect(url_for("main.dashboard"))

        return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)

    # POST — выборочная печать (для правления, с формы select) — только взносы
    if not is_board():
        abort(403)
    member_account_ids = [int(x) for x in request.form.getlist("account_id")]
    if not member_account_ids:
        flash(_("Выберите хотя бы один лицевой счёт."), "warning")
        return redirect(url_for("pd4.select"))

    coop, bank_account, slips = _build_mixed_slips(member_account_ids, [])
    if slips is None:
        return redirect(url_for("pd4.select"))

    return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)


def _print_electricity_slip(garage_id: int):
    """
    Платёжка по лицевому счёту на электричество конкретного гаража —
    доступна ТОЛЬКО текущему собственнику этого гаража (полностью или
    частично), независимо от роли. Правление не имеет административного
    доступа к печати электрических платёжек чужих гаражей — оно печатает
    только взносы (см. select()); если сам член правления владеет гаражом,
    он попадает сюда как обычный собственник, а не по признаку роли.
    """
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    owner_ids = {o.person_id for o in garage.ownerships}
    if g.user.person_id is None or g.user.person_id not in owner_ids:
        abort(403)

    if garage.account is None:
        flash(_("Лицевой счёт на электричество для этого гаража не создан."), "warning")
        return redirect(url_for("cabinet.garages"))

    coop, bank_account, slips = _build_mixed_slips([], [garage_id])
    if slips is None:
        return redirect(url_for("cabinet.garages"))

    return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)


def _electricity_account_view(garage: Garage, personal_account: PersonalAccount):
    """Адаптер PersonalAccount под интерфейс, который ожидает pd4/print.html от
    объекта account (.account_number/.person.full_name/.fee_type.name/.garage.number/.id) —
    у электрического счёта нет ни .person, ни .fee_type, т.к. он общий на гараж."""
    owners = ", ".join(o.person.full_name for o in garage.ownerships)
    payer_name = owners or _("Гараж №{n}", n=garage.number)
    electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
    fee_type_name = electricity_fee_type.name if electricity_fee_type else _("Электроэнергия")
    return type("ElectricityAccountView", (), {
        "id": personal_account.id,
        "account_number": personal_account.account_number,
        "person": type("PayerView", (), {"full_name": payer_name})(),
        "fee_type": type("FeeTypeView", (), {"name": fee_type_name})(),
        "garage": garage,
    })()


def _build_mixed_slips(member_account_ids: list[int], electricity_garage_ids: list[int]):
    """
    Общая логика построения платёжек — взносы (MemberAccount, + авто-прицепка
    пени) и электричество (по гаражам) сразу в одном списке, вперемешку.
    Права доступа должны быть уже проверены до вызова этой функции — здесь
    их больше нет, чтобы не дублировать разные правила для разных вызывающих
    (правление против рядового члена, взносы против электричества).
    Возвращает (coop, bank_account, slips); slips — список кортежей
    (view, amount, qr_data_uri, kind, ref_id), kind — 'member' | 'electricity',
    ref_id — account_id или garage_id соответственно (для повторной сборки
    того же набора на кнопке «Скачать PDF», см. print.html).
    """
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        flash(_("Сначала заполните реквизиты кооператива."), "danger")
        return None, None, None

    bank_account = get_primary_bank_account()
    slips = []

    if member_account_ids:
        accounts = database.db_session.query(MemberAccount).filter(
            MemberAccount.id.in_(member_account_ids)
        ).all()
        final_accounts = {a.id: a for a in accounts}
        for account in accounts:
            sibling = penalty_sibling_account(account)
            if sibling is not None and sibling.id not in final_accounts and balance(sibling) < 0:
                final_accounts[sibling.id] = sibling
        for account in final_accounts.values():
            debt = balance(account)
            if debt >= 0:
                continue
            amount = -debt
            qr_payload = pd4_qr_payload(coop, bank_account, account, amount)
            qr_data_uri = _qr_data_uri(qr_payload)
            database.db_session.add(PD4Document(
                account_id=account.id,
                bank_account_id=bank_account.id if bank_account else None,
                amount=amount,
                qr_payload=qr_payload,
            ))
            slips.append((account, amount, qr_data_uri, "member", account.id))

    if electricity_garage_ids:
        garages = database.db_session.query(Garage).filter(Garage.id.in_(electricity_garage_ids)).all()
        for garage in garages:
            account = garage.account
            if account is None:
                continue
            debt = balance(garage)
            if debt >= 0:
                continue
            amount = -debt
            qr_payload = pd4_qr_payload_electricity(coop, bank_account, garage, account, amount)
            qr_data_uri = _qr_data_uri(qr_payload)
            database.db_session.add(PD4Document(
                personal_account_id=account.id,
                bank_account_id=bank_account.id if bank_account else None,
                amount=amount,
                qr_payload=qr_payload,
            ))
            slips.append((_electricity_account_view(garage, account), amount, qr_data_uri, "electricity", garage.id))

    if not slips:
        flash(_("Нет задолженностей — печатать нечего."), "warning")
        return None, None, None

    database.db_session.commit()
    return coop, bank_account, slips


@bp.route("/print/pdf", methods=["GET", "POST"])
@login_required
def print_pdf():
    if request.method == "POST":
        if is_board():
            # Пост-запрос с формы select — правление, только взносы
            member_account_ids = [int(x) for x in request.form.getlist("account_id")]
            electricity_garage_ids = []
        else:
            # Пост-запрос с кнопки «Скачать PDF» на print.html — свои платёжки,
            # могут быть оба вида сразу; данные пришли от клиента — сверяем
            # каждый id с фактическим владением, а не доверяем форме вслепую.
            member_account_ids = [int(x) for x in request.form.getlist("account_id")]
            electricity_garage_ids = [int(x) for x in request.form.getlist("garage_id")]
            for aid in member_account_ids:
                account = database.db_session.get(MemberAccount, aid)
                if account is None or account.person_id != g.user.person_id:
                    abort(403)
            for gid in electricity_garage_ids:
                garage = database.db_session.get(Garage, gid)
                owner_ids = {o.person_id for o in garage.ownerships} if garage else set()
                if g.user.person_id not in owner_ids:
                    abort(403)
    else:
        # GET — авто-печать всех своих задолженностей (для рядовых), оба вида сразу
        person_id = g.user.person_id
        if person_id is None:
            flash(_("Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление."), "warning")
            return redirect(url_for("main.dashboard"))
        member_account_ids, electricity_garage_ids = _collect_member_debts(person_id)

    if not member_account_ids and not electricity_garage_ids:
        flash(_("Нет задолженностей — печатать нечего."), "info")
        return redirect(url_for("main.dashboard"))

    coop, bank_account, slips = _build_mixed_slips(member_account_ids, electricity_garage_ids)
    if slips is None:
        return redirect(url_for("main.dashboard"))

    html_str = render_template("pd4/print_pdf.html", slips=slips, coop=coop, bank_account=bank_account)

    try:
        import weasyprint
    except ImportError:
        flash(_("Для скачивания PDF нужна библиотека weasyprint. Установите: pip install weasyprint"), "danger")
        return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)

    pdf_bytes = weasyprint.HTML(string=html_str, base_url=request.url_root).write_pdf()
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=pd4.pdf"},
    )
