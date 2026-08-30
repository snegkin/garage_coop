import datetime as dt
import os
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, send_file, current_app
from werkzeug.utils import secure_filename

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import Cooperative, BankAccount, BankApiProvider, RoleEnum, Document, DocumentType
from .accounting import cooperative_balance
from .uploads import save_upload
from .permissions import is_board

bp = Blueprint("cooperative", __name__, url_prefix="/cooperative")


def _parse_decimal(raw):
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


@bp.route("/")
@login_required
def view():
    coop = database.db_session.query(Cooperative).first()
    bank_accounts = database.db_session.query(BankAccount).order_by(BankAccount.is_primary.desc(), BankAccount.bank_name).all()
    # Загружаем документы — рядовые участники видят только не-внутренние
    from .permissions import is_board
    query = database.db_session.query(Document).order_by(Document.date.desc())
    if not is_board():
        query = query.filter(Document.is_internal.is_(False))
    docs = query.all()
    return render_template("cooperative/view.html", coop=coop, bank_accounts=bank_accounts, coop_balance=cooperative_balance(), docs=docs, doc_types=DocumentType)


@bp.route("/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit():
    coop = database.db_session.query(Cooperative).first()
    if coop is None:
        coop = Cooperative(full_name="", inn="", kpp="", ogrn="")
        database.db_session.add(coop)

    if request.method == "POST":
        f = request.form
        coop.full_name = f["full_name"]
        coop.short_name = f.get("short_name") or None
        coop.inn = f["inn"]
        coop.kpp = f["kpp"]
        coop.ogrn = f["ogrn"]
        coop.legal_address = f.get("legal_address") or None
        coop.postal_address = f.get("postal_address") or None
        reg_date = f.get("registration_date")
        coop.registration_date = dt.date.fromisoformat(reg_date) if reg_date else None
        coop.total_area = _parse_decimal(f.get("total_area"))
        coop.common_area = _parse_decimal(f.get("common_area"))
        coop.cadastral_area = _parse_decimal(f.get("cadastral_area"))
        coop.cadastral_value = _parse_decimal(f.get("cadastral_value"))
        coop.standard_garage_land_area = _parse_decimal(f.get("standard_garage_land_area")) or Decimal("30")
        coop.land_tax_rate_percent = _parse_decimal(f.get("land_tax_rate_percent")) or Decimal("1.5")
        coop.bank_fee_percent = _parse_decimal(f.get("bank_fee_percent"))
        coop.dues_due_day = int(f["dues_due_day"]) if f.get("dues_due_day") else None
        coop.dues_due_month = int(f["dues_due_month"]) if f.get("dues_due_month") else None
        coop.comment = f.get("comment") or None
        database.db_session.commit()
        flash(_("Реквизиты сохранены."), "success")
        return redirect(url_for("cooperative.view"))

    return render_template("cooperative/form.html", coop=coop)


# ---------------------------------------------------------------------------
# Расчётные счета (может быть несколько, в одном или разных банках)
# ---------------------------------------------------------------------------

@bp.route("/bank-accounts/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create_bank_account():
    f = request.form
    is_primary = bool(f.get("is_primary"))
    if is_primary:
        database.db_session.query(BankAccount).update({"is_primary": False})

    try:
        api_provider = BankApiProvider(f.get("api_provider", "none"))
    except ValueError:
        abort(400)

    database.db_session.add(BankAccount(
        bank_name=f["bank_name"],
        bik=f.get("bik") or None,
        checking_account=f["checking_account"],
        correspondent_account=f.get("correspondent_account") or None,
        is_primary=is_primary,
        comment=f.get("comment") or None,
        balance=_parse_decimal(f.get("balance")),
        balance_updated_at=dt.date.fromisoformat(f["balance_updated_at"]) if f.get("balance_updated_at") else None,
        api_provider=api_provider,
    ))
    database.db_session.commit()
    flash(_("Расчётный счёт добавлен."), "success")
    return redirect(url_for("cooperative.view"))


@bp.route("/bank-accounts/<int:account_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit_bank_account(account_id):
    account = database.db_session.get(BankAccount, account_id)
    if account is None:
        abort(404)

    f = request.form
    is_primary = bool(f.get("is_primary"))
    if is_primary and not account.is_primary:
        database.db_session.query(BankAccount).filter(BankAccount.id != account.id).update({"is_primary": False})

    account.bank_name = f["bank_name"]
    account.bik = f.get("bik") or None
    account.checking_account = f["checking_account"]
    account.correspondent_account = f.get("correspondent_account") or None
    account.is_primary = is_primary
    account.comment = f.get("comment") or None
    account.balance = _parse_decimal(f.get("balance"))
    account.balance_updated_at = dt.date.fromisoformat(f["balance_updated_at"]) if f.get("balance_updated_at") else None
    try:
        account.api_provider = BankApiProvider(f.get("api_provider", "none"))
    except ValueError:
        abort(400)
    database.db_session.commit()
    flash(_("Расчётный счёт обновлён."), "success")
    return redirect(url_for("cooperative.view"))


@bp.route("/bank-accounts/<int:account_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_bank_account(account_id):
    account = database.db_session.get(BankAccount, account_id)
    if account is None:
        abort(404)
    database.db_session.delete(account)
    database.db_session.commit()
    flash(_("Расчётный счёт удалён."), "success")
    return redirect(url_for("cooperative.view"))


# ---------------------------------------------------------------------------
# Документы (перенесено из documents.py)
# ---------------------------------------------------------------------------

@bp.route("/documents")
@login_required
def list_documents():
    """Редирект на главную страницы кооператива с якорем на документы."""
    return redirect(url_for("cooperative.view") + "#documentsSection")


@bp.route("/documents/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create_document():
    if request.method == "POST":
        f = request.form
        file_storage = request.files.get("file")
        file_path = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"])

        doc = Document(
            doc_type=DocumentType(f["doc_type"]),
            number=f.get("number") or None,
            date=dt.date.fromisoformat(f["date"]),
            title=f["title"],
            file_path=file_path,
            file_name=secure_filename(file_storage.filename) if file_storage and file_storage.filename else None,
            comment=f.get("comment") or None,
            is_internal=bool(f.get("is_internal")),
        )
        database.db_session.add(doc)
        database.db_session.commit()
        flash(_("Документ сохранён."), "success")
        return redirect(url_for("cooperative.view") + "#documentsSection")

    return redirect(url_for("cooperative.view") + "#documentsSection")


@bp.route("/documents/<int:doc_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_document(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None:
        abort(404)

    f = request.form
    doc.doc_type = DocumentType(f["doc_type"])
    doc.number = f.get("number") or None
    doc.date = dt.date.fromisoformat(f["date"])
    doc.title = f["title"]
    doc.comment = f.get("comment") or None
    doc.is_internal = bool(f.get("is_internal"))

    file_storage = request.files.get("file")
    if file_storage and file_storage.filename:
        # Удаляем старый файл
        if doc.file_path and os.path.exists(doc.file_path):
            os.remove(doc.file_path)
        doc.file_path = save_upload(file_storage, current_app.config["UPLOAD_FOLDER"])
        doc.file_name = secure_filename(file_storage.filename)

    database.db_session.commit()
    flash(_("Документ обновлён."), "success")
    return redirect(url_for("cooperative.view") + "#documentsSection")


@bp.route("/documents/<int:doc_id>/file")
@login_required
def download_document(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None:
        abort(404)
    if doc.is_internal and not is_board():
        abort(403)
    if not doc.file_path:
        abort(404)
    return send_file(doc.file_path, as_attachment=True, download_name=doc.file_name)


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_document(doc_id):
    doc = database.db_session.get(Document, doc_id)
    if doc is None:
        abort(404)
    if doc.file_path and os.path.exists(doc.file_path):
        os.remove(doc.file_path)
    database.db_session.delete(doc)
    database.db_session.commit()
    flash(_("Документ удалён."), "success")
    return redirect(url_for("cooperative.view") + "#documentsSection")
