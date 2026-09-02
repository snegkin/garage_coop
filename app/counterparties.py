import datetime as dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app

from . import database
from . import audit
from .i18n import translate as _
from .auth import roles_required
from .models import (
    Counterparty, Expense, CounterpartyPayment, ReconciliationAct,
    BankAccount, Document, DocumentType, RoleEnum,
)
from .accounting import (
    counterparty_balance, reallocate_counterparty_expenses, pay_counterparty,
    expense_paid_amount, edit_counterparty_payment, reverse_counterparty_payment,
)
from .uploads import save_upload

bp = Blueprint("counterparties", __name__, url_prefix="/counterparties")


def _parse_decimal(raw):
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _save_document(
    file_key: str, title_key: str, default_title: str,
    counterparty: Counterparty, doc_type: DocumentType = DocumentType.ACT,
) -> int | None:
    """Сохраняет прикреплённый файл (если есть) как Document, возвращает document_id или None.
    Документ сразу привязывается к контрагенту (Document.counterparty_id) — чтобы
    его можно было найти фильтром по контрагенту в общем списке документов."""
    file_path = save_upload(request.files.get(file_key), current_app.config["UPLOAD_FOLDER"])
    if not file_path:
        return None
    title = request.form.get(title_key) or default_title
    doc = Document(doc_type=doc_type, date=dt.date.today(), title=title, file_path=file_path, counterparty_id=counterparty.id)
    database.db_session.add(doc)
    database.db_session.flush()
    return doc.id


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_counterparties():
    items = database.db_session.query(Counterparty).order_by(Counterparty.name).all()
    rows = [(c, counterparty_balance(c)) for c in items]
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
        opening_balance=_parse_decimal(f.get("opening_balance")),
        opening_balance_date=dt.date.fromisoformat(f["opening_balance_date"]) if f.get("opening_balance_date") else None,
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
    counterparty.opening_balance = _parse_decimal(f.get("opening_balance"))
    opening_date = f.get("opening_balance_date")
    counterparty.opening_balance_date = dt.date.fromisoformat(opening_date) if opening_date else None
    database.db_session.commit()
    flash(_("Данные контрагента обновлены."), "success")
    return redirect(url_for("counterparties.list_counterparties"))


@bp.route("/<int:counterparty_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    if counterparty.expenses or counterparty.payments:
        flash(_("Нельзя удалить контрагента — по нему есть записи о расходах или платежах."), "danger")
        return redirect(url_for("counterparties.list_counterparties"))

    database.db_session.delete(counterparty)
    database.db_session.commit()
    flash(_("Контрагент удалён."), "success")
    return redirect(url_for("counterparties.list_counterparties"))


# ---------------------------------------------------------------------------
# Карточка контрагента: расходы, платежи, акты сверки
# ---------------------------------------------------------------------------

@bp.route("/<int:counterparty_id>")
@roles_required(RoleEnum.BOARD)
def detail(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    expenses = sorted(counterparty.expenses, key=lambda e: e.date, reverse=True)
    payments = sorted(counterparty.payments, key=lambda p: p.date, reverse=True)
    acts = sorted(counterparty.reconciliation_acts, key=lambda a: a.period_end, reverse=True)
    bank_accounts = database.db_session.query(BankAccount).order_by(BankAccount.is_primary.desc(), BankAccount.bank_name).all()

    expense_paid = {e.id: expense_paid_amount(e) for e in expenses}
    originals = [p for p in counterparty.payments if p.reverses_payment_id is None]
    last_payment_id = max(originals, key=lambda p: (p.date, p.id)).id if originals else None

    return render_template(
        "counterparties/detail.html",
        counterparty=counterparty,
        balance=counterparty_balance(counterparty),
        expenses=expenses,
        payments=payments,
        acts=acts,
        bank_accounts=bank_accounts,
        expense_paid=expense_paid,
        last_payment_id=last_payment_id,
        today=dt.date.today(),
    )


@bp.route("/<int:counterparty_id>/expenses/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_expense(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    f = request.form
    document_id = _save_document(
        "document_file", "document_title",
        _("Счёт от {name}", name=counterparty.name), counterparty,
    )
    database.db_session.add(Expense(
        counterparty_id=counterparty.id,
        date=dt.date.fromisoformat(f["date"]),
        amount=Decimal(f["amount"]),
        category=f.get("category") or None,
        description=f.get("description") or None,
        document_id=document_id,
    ))
    database.db_session.flush()
    reallocate_counterparty_expenses(counterparty)
    audit.record(
        "expense.create", entity_type="counterparty", entity_id=counterparty.id,
        summary=f"Расход {f['amount']} — {counterparty.name}, {f['date']}",
    )
    database.db_session.commit()
    flash(_("Расход добавлен."), "success")
    return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))


@bp.route("/<int:counterparty_id>/payments/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_payment(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    f = request.form
    bank_account_id = f.get("bank_account_id")
    bank_account = database.db_session.get(BankAccount, int(bank_account_id)) if bank_account_id else None
    document_id = _save_document(
        "document_file", "document_title",
        _("Платёжный документ — {name}", name=counterparty.name), counterparty,
    )
    pay_counterparty(
        counterparty=counterparty,
        date=dt.date.fromisoformat(f["date"]),
        amount=Decimal(f["amount"]),
        bank_account=bank_account,
        document_id=document_id,
        comment=f.get("comment") or None,
    )
    audit.record(
        "counterparty_payment.create", entity_type="counterparty", entity_id=counterparty.id,
        summary=f"Платёж контрагенту {f['amount']} — {counterparty.name}, {f['date']}",
    )
    database.db_session.commit()
    flash(_("Платёж добавлен."), "success")
    return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))


@bp.route("/<int:counterparty_id>/payments/<int:payment_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_payment(counterparty_id, payment_id):
    """
    Правка платежа — только для случая, когда ошиблись в сумме/дате при
    вводе. Разрешена только для последнего (по дате, среди не-сторно)
    платежа контрагента — чтобы не переписывать задним числом историю,
    для этого есть сторно (см. reverse_payment). Сам платёж не должен быть
    отменяющей проводкой и не должен быть уже сторнирован.
    """
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    payment = database.db_session.get(CounterpartyPayment, payment_id)
    if counterparty is None or payment is None or payment.counterparty_id != counterparty.id:
        abort(404)

    originals = [p for p in counterparty.payments if p.reverses_payment_id is None]
    last_payment = max(originals, key=lambda p: (p.date, p.id)) if originals else None
    if last_payment is None or payment.id != last_payment.id:
        flash(_("Редактировать можно только последний платёж контрагенту."), "danger")
        return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))
    if payment.reverses_payment_id is not None:
        flash(_("Это отменяющая проводка (сторно) — её нельзя редактировать."), "danger")
        return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))
    if payment.reversed_by:
        flash(_("Этот платёж уже сторнирован — редактировать его нельзя."), "danger")
        return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))

    f = request.form
    bank_account_id = f.get("bank_account_id")
    bank_account = database.db_session.get(BankAccount, int(bank_account_id)) if bank_account_id else None
    document_id = _save_document(
        "document_file", "document_title",
        _("Платёжный документ — {name}", name=counterparty.name), counterparty,
    )
    edit_counterparty_payment(
        payment=payment,
        date=dt.date.fromisoformat(f["date"]),
        amount=Decimal(f["amount"]),
        bank_account=bank_account,
        document_id=document_id,
        comment=f.get("comment") or None,
    )
    audit.record(
        "counterparty_payment.edit", entity_type="counterparty_payment", entity_id=payment.id,
        summary=f"Изменён платёж контрагенту #{payment.id} — {counterparty.name}, новая сумма {f['amount']}",
    )
    database.db_session.commit()
    flash(_("Платёж изменён."), "success")
    return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))


@bp.route("/<int:counterparty_id>/payments/<int:payment_id>/reverse", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def reverse_payment(counterparty_id, payment_id):
    """
    Отменяющая проводка (сторно) — для платежа, который реально ушёл в
    банк с ошибкой в реквизитах/организации, а потом вернулся. В отличие
    от правки, разрешена для ЛЮБОГО платежа (не только последнего) — ведь
    возврат могут обнаружить не сразу, а сам факт «платёж уходил» должен
    остаться в истории.
    """
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    payment = database.db_session.get(CounterpartyPayment, payment_id)
    if counterparty is None or payment is None or payment.counterparty_id != counterparty.id:
        abort(404)
    if payment.reverses_payment_id is not None:
        flash(_("Это уже отменяющая проводка (сторно) — сторнировать сторно нельзя."), "danger")
        return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))
    if payment.reversed_by:
        flash(_("Этот платёж уже сторнирован."), "danger")
        return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))

    f = request.form
    reverse_date = dt.date.fromisoformat(f["date"]) if f.get("date") else dt.date.today()
    reverse_counterparty_payment(
        payment=payment,
        date=reverse_date,
        comment=f.get("comment") or None,
    )
    audit.record(
        "counterparty_payment.reverse", entity_type="counterparty_payment", entity_id=payment.id,
        summary=f"Сторно платежа #{payment.id} — {counterparty.name}, дата сторно {reverse_date.isoformat()}",
    )
    database.db_session.commit()
    flash(_("Отменяющая проводка добавлена."), "success")
    return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))


@bp.route("/<int:counterparty_id>/reconciliation/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_reconciliation_act(counterparty_id):
    counterparty = database.db_session.get(Counterparty, counterparty_id)
    if counterparty is None:
        abort(404)

    f = request.form
    document_id = _save_document(
        "document_file", "document_title",
        _("Акт сверки — {name}", name=counterparty.name), counterparty,
    )
    database.db_session.add(ReconciliationAct(
        counterparty_id=counterparty.id,
        period_start=dt.date.fromisoformat(f["period_start"]),
        period_end=dt.date.fromisoformat(f["period_end"]),
        our_balance=_parse_decimal(f.get("our_balance")) if f.get("our_balance") else counterparty_balance(counterparty),
        counterparty_balance=_parse_decimal(f.get("counterparty_balance")),
        document_id=document_id,
        comment=f.get("comment") or None,
    ))
    database.db_session.commit()
    flash(_("Акт сверки добавлен."), "success")
    return redirect(url_for("counterparties.detail", counterparty_id=counterparty.id))
