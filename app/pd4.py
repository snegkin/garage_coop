import base64
import io
from decimal import Decimal

import segno
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, Response

from . import database
from .i18n import translate as _
from .auth import login_required
from .models import MemberAccount, Person, PD4Document, Cooperative, RoleEnum, Garage, PersonalAccount, FeeType
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


def _is_board() -> bool:
    return g.user.role.value in ("chairman", "accountant", "board")

def _is_owner_or_board(garage: Garage) -> bool:
    """Правление/председатель — любой гараж; рядовой член — только свой (по владению)."""
    if _is_board():
        return True
    if g.user.person_id is None:
        return False
    owner_ids = {o.person_id for o in garage.ownerships}
    return g.user.person_id in owner_ids

def _is_privileged() -> bool:
    """Председатель/бухгалтер — видят все счета и строки «пеня»; рядовой член правления (board) — нет."""
    return g.user.role.value in ("chairman", "accountant")

@bp.route("/")
@login_required
def select():
    """Полный список счетов с поиском (только правление/председатель)."""
    if not _is_board():
        return redirect(url_for("pd4.print_slips"))

    privileged = _is_privileged()
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

    return render_template("pd4/select.html", rows=rows, all_persons=all_persons, is_board=_is_board(), q=q)


def _collect_account_ids(person_id: int) -> list[int]:
    """Собирает все ID счетов члена, у которых есть задолженность."""
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


@bp.route("/print", methods=["GET", "POST"])
@login_required
def print_slips():
    # платёжка по электричеству конкретного гаража (кнопка «Оплатить» в /cabinet/garages)
    garage_id = request.values.get("garage_id", type=int)
    if garage_id is not None:
        return _print_electricity_slip(garage_id)

    # GET — печать по конкретному счёту (кнопка «Оплатить») либо по всем долгам текущего пользователя
    if request.method == "GET":
        account_id = request.args.get("account_id", type=int)
        if account_id is not None:
            account = database.db_session.get(MemberAccount, account_id)
            if account is None:
                abort(404)
            if not _is_board() and account.person_id != g.user.person_id:
                abort(403)
            account_ids = [account_id]
        else:
            person_id = g.user.person_id
            if person_id is None:
                flash(_("Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление."), "warning")
                return redirect(url_for("main.dashboard"))
            # все счета, у которых есть долг
            account_ids = _collect_account_ids(person_id)

        if not account_ids:
            flash(_("Нет задолженностей — печатать нечего."), "info")
            return redirect(url_for("main.dashboard"))

        coop, bank_account, slips = _build_slips(account_ids)
        if slips is None:
            return redirect(url_for("main.dashboard"))

        return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account, electricity=False)

    # POST — выборочная печать (для правления, с формы select)
    if not _is_board():
        abort(403)
    account_ids = [int(x) for x in request.form.getlist("account_id")]
    if not account_ids:
        flash(_("Выберите хотя бы один лицевой счёт."), "warning")
        return redirect(url_for("pd4.select"))

    coop, bank_account, slips = _build_slips(account_ids)
    if slips is None:
        return redirect(url_for("pd4.select"))

    return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account, electricity=False)


def _print_electricity_slip(garage_id: int):
    """Платёжка по лицевому счёту на электричество конкретного гаража — доступна
    правлению или любому текущему собственнику этого гаража."""
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not _is_board():
        owner_ids = {o.person_id for o in garage.ownerships}
        if g.user.person_id is None or g.user.person_id not in owner_ids:
            abort(403)

    account = garage.account
    if account is None:
        flash(_("Лицевой счёт на электричество для этого гаража не создан."), "warning")
        return redirect(url_for("cabinet.garages"))

    debt = balance(garage)
    if debt >= 0:
        flash(_("Нет задолженностей — печатать нечего."), "info")
        return redirect(url_for("cabinet.garages"))

    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        flash(_("Сначала заполните реквизиты кооператива."), "danger")
        return redirect(url_for("cabinet.garages"))

    bank_account = get_primary_bank_account()
    amount = -debt
    qr_payload = pd4_qr_payload_electricity(coop, bank_account, garage, account, amount)
    qr_data_uri = _qr_data_uri(qr_payload)
    database.db_session.add(PD4Document(
        personal_account_id=account.id,
        bank_account_id=bank_account.id if bank_account else None,
        amount=amount,
        qr_payload=qr_payload,
    ))
    database.db_session.commit()

    view = _electricity_account_view(garage, account)
    slips = [(view, amount, qr_data_uri)]
    return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account, electricity=True)


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


def _build_slips(account_ids: list[int]):
    """Общая логика: права, авто-прицепка пени, QR, сохранение истории."""
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        flash(_("Сначала заполните реквизиты кооператива."), "danger")
        return None, None, None

    accounts = database.db_session.query(MemberAccount).filter(
        MemberAccount.id.in_(account_ids)
    ).all()

    if not _is_board():
        for account in accounts:
            if account.person_id != g.user.person_id:
                abort(403)

    final_accounts = {a.id: a for a in accounts}
    for account in accounts:
        sibling = penalty_sibling_account(account)
        if sibling is not None and sibling.id not in final_accounts and balance(sibling) < 0:
            final_accounts[sibling.id] = sibling

    bank_account = get_primary_bank_account()

    slips = []
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
        slips.append((account, amount, qr_data_uri))

    if not slips:
        flash(_("Нет задолженностей — печатать нечего."), "warning")
        return None, None, None

    database.db_session.commit()
    return coop, bank_account, slips


@bp.route("/print/pdf", methods=["GET", "POST"])
@login_required
def print_pdf():
    if request.method == "POST":
        # Пост-запрос с формы select — для правления
        account_ids = [int(x) for x in request.form.getlist("account_id")] if _is_board() else _collect_account_ids(g.user.person_id)
    else:
        # GET — авто-печать всех счетов (для рядовых)
        person_id = g.user.person_id
        if person_id is None:
            flash(_("Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление."), "warning")
            return redirect(url_for("main.dashboard"))
        account_ids = _collect_account_ids(person_id)

    if not account_ids:
        flash(_("Нет задолженностей — печатать нечего."), "info")
        return redirect(url_for("main.dashboard"))

    coop, bank_account, slips = _build_slips(account_ids)
    if slips is None:
        return redirect(url_for("main.dashboard"))

    html_str = render_template("pd4/print_pdf.html", slips=slips, coop=coop, bank_account=bank_account)

    try:
        import weasyprint
    except ImportError:
        flash(_("Для скачивания PDF нужна библиотека weasyprint. Установите: pip install weasyprint"), "danger")
        return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account, electricity=False)

    pdf_bytes = weasyprint.HTML(string=html_str, base_url=request.url_root).write_pdf()
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=pd4.pdf"},
    )
