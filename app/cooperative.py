import datetime as dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import Cooperative, BankAccount, BankApiProvider, RoleEnum
from .accounting import cooperative_balance

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
    return render_template("cooperative/view.html", coop=coop, bank_accounts=bank_accounts, coop_balance=cooperative_balance())


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
