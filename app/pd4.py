import base64
import io
from decimal import Decimal

import segno
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, Response

from . import database
from .i18n import translate as _
from .auth import login_required
from .models import MemberAccount, Person, PD4Document, Cooperative, RoleEnum, Garage
from .accounting import balance, penalty_sibling_account, get_primary_bank_account, pd4_qr_payload

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
    # GET — авто-печать всех счетов текущего пользователя
    if request.method == "GET":
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

        return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)

    # POST — выборочная печать (для правления, с формы select)
    account_ids = [int(x) for x in request.form.getlist("account_id")]
    if not account_ids:
        flash(_("Выберите хотя бы один лицевой счёт."), "warning")
        return redirect(url_for("pd4.select"))

    coop, bank_account, slips = _build_slips(account_ids)
    if slips is None:
        return redirect(url_for("pd4.select"))

    return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)


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
        account_ids = [int(x) for x in request.form.getlist("account_id")]
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
        return render_template("pd4/print.html", slips=slips, coop=coop, bank_account=bank_account)

    pdf_bytes = weasyprint.HTML(string=html_str, base_url=request.url_root).write_pdf()
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=pd4.pdf"},
    )
