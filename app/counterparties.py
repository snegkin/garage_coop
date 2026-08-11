from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .models import Counterparty, RoleEnum

bp = Blueprint("counterparties", __name__, url_prefix="/counterparties")


def _balance(counterparty: Counterparty) -> Decimal:
    """Сумма расходов кооператива в адрес этого контрагента (сколько всего ему заплачено)."""
    return -sum((e.amount for e in counterparty.expenses), Decimal("0"))


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_counterparties():
    items = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    rows = [(c, _balance(c)) for c in items]
    return render_template("counterparties/list.html", rows=rows)


@bp.route("/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def create():
    f = request.form
    counterparty = Counterparty(
        name=f["name"],
        inn=f.get("inn") or None,
        kpp=f.get("kpp") or None,
        category=f.get("category") or None,
        phone=f.get("phone") or None,
        email=f.get("email") or None,
        address=f.get("address") or None,
        comment=f.get("comment") or None,
    )
    database.db_session.add(counterparty)
    database.db_session.commit()
    flash(_("Контрагент добавлен."), "success")
    return redirect(url_for("counterparties.list_counterparties"))


@bp.route("/<int:counterparty_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    f = request.form
    counterparty.name = f["name"]
    counterparty.inn = f.get("inn") or None
    counterparty.kpp = f.get("kpp") or None
    counterparty.category = f.get("category") or None
    counterparty.phone = f.get("phone") or None
    counterparty.email = f.get("email") or None
    counterparty.address = f.get("address") or None
    counterparty.comment = f.get("comment") or None
    database.db_session.commit()
    flash(_("Данные контрагента обновлены."), "success")
    return redirect(url_for("counterparties.list_counterparties"))


@bp.route("/<int:counterparty_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    if counterparty.expenses:
        flash(_("Нельзя удалить контрагента — по нему есть записи о расходах."), "danger")
        return redirect(url_for("counterparties.list_counterparties"))

    database.db_session.delete(counterparty)
    database.db_session.commit()
    flash(_("Контрагент удалён."), "success")
    return redirect(url_for("counterparties.list_counterparties"))
