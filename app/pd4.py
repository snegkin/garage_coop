import base64
import io
from decimal import Decimal

import segno
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, Response

from . import database
from .i18n import translate as _
from .auth import login_required
from .models import MemberAccount, Person, PD4Document, Cooperative, RoleEnum, PersonDataRevisionStatus
from .accounting import balance, penalty_sibling_account, get_primary_bank_account, pd4_qr_payload

bp = Blueprint("pd4", __name__, url_prefix="/pd4")


def _qr_data_uri(payload: str) -> str:
    """PNG QR-кода в виде self-contained base64 data URI для <img src="...">."""
    qr = segno.make(payload, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=2, border=1, dark="black", light="white")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _is_privileged() -> bool:
    """Печатать на любого члена или на всех разом может только председатель и бухгалтер —
    остальные (включая рядового члена правления) видят и печатают только свои счета."""
    return g.user.role in (RoleEnum.CHAIRMAN, RoleEnum.ACCOUNTANT)


@bp.route("/")
@login_required
def select():
    privileged = _is_privileged()
    all_persons = []

    query = database.db_session.query(MemberAccount)
    if privileged:
        all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
        filter_person_id = request.args.get("person_id", type=int)
        if filter_person_id:
            query = query.filter(MemberAccount.person_id == filter_person_id)
    else:
        if g.user.person_id is None:
            flash(_("Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление."), "warning")
            return render_template("pd4/select.html", rows=[], all_persons=[], is_privileged=False)
        query = query.filter(MemberAccount.person_id == g.user.person_id)

    rows = []
    for account in query.all():
        # рядовым членам не показываем счета пени отдельной строкой — при печати они прицепятся сами
        if not privileged and account.fee_type.is_penalty:
            continue
        debt = balance(account)
        if debt < 0:
            rows.append((account, debt))
    rows.sort(key=lambda r: (r[0].person.full_name, r[0].garage.number))

    return render_template("pd4/select.html", rows=rows, all_persons=all_persons, is_privileged=privileged)


def _build_slips(account_ids: list[int]):
    """Общая логика для печати в браузере и для скачивания PDF: проверяет права,
    авто-прицепляет счета пени, формирует QR и сохраняет историю ПД-4.
    Возвращает (coop, bank_account, slips) либо кидает redirect через flash+None."""
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        flash(_("Сначала заполните реквизиты кооператива."), "danger")
        return None, None, None

    accounts = database.db_session.query(MemberAccount).filter(MemberAccount.id.in_(account_ids)).all()

    if not _is_privileged():
        for account in accounts:
            if account.person_id != g.user.person_id:
                abort(403)

    # авто-прицепка счёта пени, если он ненулевой — даже если его не выбирали явно
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
        flash(_("По выбранным счетам нет задолженности — печатать нечего."), "warning")
        return None, None, None

    database.db_session.commit()
    return coop, bank_account, slips


@bp.route("/print", methods=["POST"])
@login_required
def print_slips():
    account_ids = [int(x) for x in request.form.getlist("account_id")]
    if not account_ids:
        flash(_("Выберите хотя бы один лицевой счёт."), "warning")
        return redirect(url_for("pd4.select"))

    coop, bank_account, slips = _build_slips(account_ids)
    if slips is None:
        return redirect(url_for("pd4.select"))

    return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)


@bp.route("/print/pdf", methods=["POST"])
@login_required
def print_pdf():
    account_ids = [int(x) for x in request.form.getlist("account_id")]
    if not account_ids:
        flash(_("Выберите хотя бы один лицевой счёт."), "warning")
        return redirect(url_for("pd4.select"))

    coop, bank_account, slips = _build_slips(account_ids)
    if slips is None:
        return redirect(url_for("pd4.select"))

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
