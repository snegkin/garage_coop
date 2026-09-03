import datetime as dt
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from sqlalchemy import or_

from . import database
from . import audit
from .i18n import translate as _, fmt2, parse_decimal
from .auth import login_required, roles_required
from .permissions import can_view_member_account, is_board, is_privileged
from .models import (
    GarageOwnership, Charge, Payment, Garage, PersonalAccount,
    FeeType, MemberAccount, Person, RoleEnum,
    Cooperative, BankAccount,
)
from .accounting import (
    get_settings, electricity_account_number, member_account_number, owner_index_for, balance as _balance,
    compute_land_tax, reallocate_member_charges,
)

bp = Blueprint("finance", __name__, url_prefix="/finance")


# ---------------------------------------------------------------------------
# Расчётные счета кооператива (реквизиты, баланс, интеграция с банком —
# сами счета создаются/редактируются на этой странице, роуты создания
# остались в cooperative.py вместе с остальными реквизитами кооператива)
# ---------------------------------------------------------------------------

@bp.route("/bank-accounts")
@roles_required(RoleEnum.BOARD)
def bank_accounts():
    accounts = (
        database.db_session.query(BankAccount)
        .order_by(BankAccount.is_primary.desc(), BankAccount.bank_name)
        .all()
    )
    return render_template("finance/bank_accounts.html", bank_accounts=accounts)


# ---------------------------------------------------------------------------
# Виды взносов
# ---------------------------------------------------------------------------

@bp.route("/fee-types")
@roles_required(RoleEnum.BOARD)
def fee_types():
    types = database.db_session.query(FeeType).order_by(FeeType.name).all()
    return render_template("finance/fee_types.html", types=types)


@bp.route("/fee-types/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create_fee_type():
    f = request.form
    database.db_session.add(FeeType(
        code=f["code"],
        name=f["name"],
        comment=f.get("comment") or None,
        type_code=f.get("type_code") or None,
        is_penalty=bool(f.get("is_penalty")),
    ))
    database.db_session.commit()
    flash(_("Вид взноса добавлен."), "success")
    return redirect(url_for("finance.fee_types"))


# ---------------------------------------------------------------------------
# Лицевые счета на электричество (по гаражу)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Лицевые счета членов кооператива (земельный налог, взносы, пени — по гаражу и виду взноса)
# ---------------------------------------------------------------------------

def _account_stats(rows: list[tuple[MemberAccount, Decimal]]) -> dict:
    """Статистика по видам счетов для finance/member_accounts.html — считается
    из уже посчитанных балансов (rows), без отдельных SQL-запросов: баланс
    каждого счёта и так вычисляется для таблицы.

    Архивные счета (закрываются при смене собственника гаража, см. миграцию
    archive member accounts on ownership transfer) в статистику не входят —
    тот же смысл "долга", что и на дашборде (main.py:_debt_summary)."""
    by_type: dict[int, dict] = {}
    for account, balance in rows:
        if account.is_archived:
            continue
        ft = account.fee_type
        stat = by_type.setdefault(ft.id, {
            "name": ft.name, "count": 0, "debt_count": 0,
            "total_debt": Decimal("0"), "total_balance": Decimal("0"),
        })
        stat["count"] += 1
        stat["total_balance"] += balance
        if balance < 0:
            stat["debt_count"] += 1
            stat["total_debt"] += balance
    by_type_list = sorted(by_type.values(), key=lambda s: s["name"])
    return {
        "by_type": by_type_list,
        "total_count": sum(s["count"] for s in by_type_list),
        "total_debt_accounts": sum(s["debt_count"] for s in by_type_list),
        "total_debt": sum((s["total_debt"] for s in by_type_list), Decimal("0")),
        "total_balance": sum((s["total_balance"] for s in by_type_list), Decimal("0")),
    }


@bp.route("/member-accounts")
@roles_required(RoleEnum.BOARD)
def member_accounts():
    accs = (
        database.db_session.query(MemberAccount)
        .join(Person)
        .order_by(Person.full_name, MemberAccount.account_number)
        .all()
    )
    rows = [(a, _balance(a)) for a in accs]
    # Кнопка «Пеня» (Удалить/Списать) над таблицей — только если есть что
    # удалять/списывать, см. write_off_all_penalties/delete_all_penalties.
    has_unpaid_penalty = any(a.fee_type.is_penalty and bal < 0 for a, bal in rows)
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    all_garages = database.db_session.query(Garage).order_by(Garage.number).all()
    all_fee_types = database.db_session.query(FeeType).order_by(FeeType.name).all()
    return render_template(
        "finance/member_accounts.html", rows=rows, has_unpaid_penalty=has_unpaid_penalty,
        all_persons=all_persons, all_garages=all_garages, all_fee_types=all_fee_types,
        account_stats=_account_stats(rows),
    )


@bp.route("/member-accounts/<int:account_id>")
@login_required
def member_account_detail(account_id):
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        flash(_("Лицевой счёт не найден."), "danger")
        return redirect(url_for("finance.member_accounts"))
    if not can_view_member_account(account):
        abort(403)

    # предыдущий/следующий счёт (по ID, как на странице гаража) —
    # рядовому члену показываем только его собственные счета (иначе
    # «Вперёд» почти наверняка выведет на чужой счёт и упрётся в 403 —
    # у member'а обычно 1-2 счёта на весь список, ID глобальный по всей
    # таблице); правлению/бухгалтеру/председателю — по всем счетам сразу,
    # как и на самой странице списка счетов, которую они видят целиком.
    nav_query = database.db_session.query(MemberAccount)
    if not is_board():
        nav_query = nav_query.filter(MemberAccount.person_id == g.user.person_id)
    prev_account = nav_query.filter(MemberAccount.id < account.id).order_by(MemberAccount.id.desc()).first()
    next_account = nav_query.filter(MemberAccount.id > account.id).order_by(MemberAccount.id).first()

    # Счета, на которые можно зачесть средства с этого (см.
    # transfer_member_account_funds) — того же человека или того же
    # гаража, не произвольные чужие счета.
    transferable_accounts = (
        database.db_session.query(MemberAccount)
        .join(Person, MemberAccount.person_id == Person.id)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(
            MemberAccount.id != account.id,
            or_(MemberAccount.person_id == account.person_id, MemberAccount.garage_id == account.garage_id),
        )
        .order_by(MemberAccount.account_number)
        .all()
    )
    # Баланс каждого счёта-получателя — чтобы видеть в модалке зачёта, куда
    # реально уходят деньги, не открывая отдельно каждый счёт.
    transferable_balances = {t.id: _balance(t) for t in transferable_accounts}

    return render_template(
        "finance/member_account_detail.html", account=account, balance=_balance(account),
        prev_account=prev_account, next_account=next_account, today=dt.date.today(),
        transferable_accounts=transferable_accounts, transferable_balances=transferable_balances,
    )


@bp.route("/member-accounts/<int:account_id>/number", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_member_account_number(account_id):
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    account.account_number = request.form["account_number"].strip()
    try:
        database.db_session.commit()
    except Exception:
        database.db_session.rollback()
        flash(_("Такой номер счёта уже используется."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))
    flash(_("Номер счёта обновлён."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/<int:account_id>/number/default")
@roles_required(RoleEnum.BOARD)
def suggest_member_account_number(account_id):
    """
    Считает номер счёта «по умолчанию» — по той же формуле и с теми же
    настройками (AccountNumberSettings), что при автосоздании счёта и при
    массовом пересчёте (см. accounting.member_account_number/owner_index_for,
    finance._regenerate_account_numbers) — не выдумывает отдельное правило.
    Только СЧИТАЕТ и отдаёт JSON, ничего не сохраняет — кнопка «По
    умолчанию» на странице счёта просто подставляет результат в поле
    ввода, реальное сохранение по-прежнему идёт через обычную форму/роут
    update_member_account_number (там же и проверка на занятый номер) —
    председатель успевает посмотреть и поправить перед сохранением, а не
    сохраняет вслепую одним кликом.
    """
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    if not account.fee_type.type_code:
        return {
            "error": _("У вида взноса «{name}» нет кода для формулы номера — задайте номер вручную.")
            .format(name=account.fee_type.name),
        }
    owner_index = owner_index_for(account.garage_id, account.person_id)
    account_number = member_account_number(
        account.fee_type.type_code, account.garage_id, owner_index, account.fee_type.is_penalty,
    )
    return {"account_number": account_number}


@bp.route("/member-accounts/<int:account_id>/charges/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_member_charge(account_id):
    """
    Тоже вызывается через AJAX — с кнопки прямо в сводной таблице «Лицевые
    счета» на странице гаража/человека (см. garages/detail.html,
    persons/detail.html), не только с полноценной страницы счёта: заходить
    на отдельную страницу счёта ради одного начисления неудобно. Роут
    отдаёт JSON при заголовке X-Requested-With — тот же приём, что и в
    bank_sync.py (allocate_statement_line и т.п.); обычная форма (JS
    отключён, либо форма на самой странице счёта) по-прежнему работает
    через redirect+flash.
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def respond(success: bool, message: str, **extra):
        if is_ajax:
            return {"success": success, "message": message, **extra}
        flash(message, "success" if success else "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account_id))

    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    f = request.form
    try:
        year = int(f["year"])
        amount = parse_decimal(f["amount"])
    except (KeyError, ValueError, InvalidOperation):
        return respond(False, _("Проверьте год и сумму начисления."))
    if amount <= 0:
        return respond(False, _("Сумма начисления должна быть больше нуля."))

    database.db_session.add(Charge(
        account_id=account.id, year=year, amount=amount, comment=f.get("comment") or None,
    ))
    database.db_session.flush()
    reallocate_member_charges(account)
    audit.record(
        "charge.create", entity_type="member_account", entity_id=account.id,
        summary=f"Начисление {audit.format_amount(amount)} на счёт {account.account_number} "
                f"({account.person.short_name}), {year} год",
    )
    database.db_session.commit()
    new_balance = _balance(account)
    return respond(
        True, _("Начисление добавлено."),
        balance=fmt2(new_balance), balance_negative=new_balance < 0,
    )


@bp.route("/member-accounts/<int:account_id>/charges/<int:charge_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_member_charge(account_id, charge_id):
    """
    Правка уже проведённого начисления — например, когда массовое
    начисление (mass_charge) посчитало сумму по формуле, которая для
    части счетов (в частности, земельный налог по приватизированным
    гаражам за отдельные годы) не совпадает с правильным расчётом, и
    ошибку нужно исправить точечно, не откатывая всё начисление целиком.
    Разрешена правка любого начисления счёта, а не только последнего
    (в отличие от counterparties.edit_payment) — типичный случай здесь
    как раз в том, что ошибка вскрывается для начислений прошлых лет.
    """
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    charge = database.db_session.get(Charge, charge_id)
    if charge is None or charge.account_id != account.id:
        abort(404)

    f = request.form
    try:
        year = int(f["year"])
        amount = parse_decimal(f["amount"])
    except (KeyError, ValueError, InvalidOperation):
        flash(_("Проверьте год и сумму начисления."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))
    if amount <= 0:
        flash(_("Сумма начисления должна быть больше нуля."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))

    old_amount = charge.amount
    charge.year = year
    charge.amount = amount
    charge.comment = f.get("comment") or None
    database.db_session.flush()
    reallocate_member_charges(account)
    audit.record(
        "charge.edit", entity_type="member_account", entity_id=account.id,
        summary=f"Начисление на счёте {account.account_number} ({account.person.short_name}) изменено: "
                f"{audit.format_amount(old_amount)} → {audit.format_amount(amount)}, {year} год",
    )
    database.db_session.commit()
    flash(_("Начисление изменено."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/<int:account_id>/charges/<int:charge_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_member_charge(account_id, charge_id):
    """
    Удаление ошибочно проведённого начисления. Уровень доступа — CHAIRMAN,
    как и у delete_member_payment (не is_board(), как у создания/правки) —
    удаление финансовой записи чувствительнее, чем правка суммы.

    Если это начисление — «источник» зачёта между счетами (см.
    transfer_member_account_funds/cancel_transfer, Payment.offset_charge_id
    ссылается именно на такое начисление) — прямое удаление здесь
    заблокировано: SET NULL на offset_charge_id молча оборвал бы связь, и
    платёж на другом счёте остался бы висеть, как будто он не имеет
    отношения к зачёту, а долг на этом счёте пропал бы без следа. Нужно
    использовать «Отменить зачёт» на платеже — она удаляет обе половины
    разом.
    """
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    charge = database.db_session.get(Charge, charge_id)
    if charge is None or charge.account_id != account.id:
        abort(404)
    linked_payment = database.db_session.query(Payment).filter_by(offset_charge_id=charge.id).first()
    if linked_payment is not None:
        flash(
            _("Это начисление — часть зачёта между счетами. Используйте «Отменить зачёт» на платеже "
              "(счёт {number}).").format(number=linked_payment.account.account_number if linked_payment.account else "?"),
            "danger",
        )
        return redirect(url_for("finance.member_account_detail", account_id=account.id))

    audit.record(
        "charge.delete", entity_type="member_account", entity_id=account.id,
        summary=f"Удалено начисление {audit.format_amount(charge.amount)} за {charge.year} год на счёте "
                f"{account.account_number} ({account.person.short_name})",
    )
    database.db_session.delete(charge)
    database.db_session.flush()
    reallocate_member_charges(account)
    database.db_session.commit()
    flash(_("Начисление удалено."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/<int:account_id>/payments/add", methods=["POST"])
@login_required
def add_member_payment(account_id):
    """Та же AJAX-поддержка, что и у add_member_charge выше — см. её докстринг."""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def respond(success: bool, message: str, **extra):
        if is_ajax:
            return {"success": success, "message": message, **extra}
        flash(message, "success" if success else "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account_id))

    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    if not is_board():
        abort(403)
    f = request.form
    try:
        date = dt.date.fromisoformat(f["date"])
    except (KeyError, ValueError):
        return respond(False, _("Проверьте дату платежа."))

    amount_raw = (f.get("amount") or "").strip()
    if amount_raw:
        try:
            amount = parse_decimal(amount_raw)
        except InvalidOperation:
            return respond(False, _("Проверьте сумму платежа."))
    else:
        # Пустое поле — закрыть текущий долг полностью (см. placeholder в
        # форме, показывающий именно эту сумму); если долга нет, пустое
        # поле ничего не означает — сумму нужно указать явно.
        current_balance = _balance(account)
        if current_balance >= 0:
            return respond(False, _("Укажите сумму платежа — на счёте нет долга, чтобы закрыть его пустым полем."))
        amount = -current_balance
    if amount <= 0:
        return respond(False, _("Сумма платежа должна быть больше нуля."))

    database.db_session.add(Payment(
        account_id=account.id, date=date, amount=amount, comment=f.get("comment") or None,
    ))
    database.db_session.flush()
    reallocate_member_charges(account)
    audit.record(
        "payment.create", entity_type="member_account", entity_id=account.id,
        summary=f"Платёж {audit.format_amount(amount)} на счёт {account.account_number} "
                f"({account.person.short_name}) от {audit.format_date(date)}",
    )
    database.db_session.commit()
    new_balance = _balance(account)
    return respond(
        True, _("Платёж зарегистрирован."),
        balance=fmt2(new_balance), balance_negative=new_balance < 0,
    )


@bp.route("/member-accounts/<int:account_id>/payments/<int:payment_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_member_payment(account_id, payment_id):
    """
    Удаление ошибочно/задвоенно внесённого платежа. Если платёж был
    сопоставлен со строкой банковской выписки или записью реестра платежей
    (BankStatementLine.matched_payment_id / PaymentRegistryEntry.
    matched_payment_id, оба ondelete="SET NULL") — при удалении эта ссылка
    сама обнулится (внешние ключи в проекте включены на каждое соединение,
    см. app/database.py), сама строка выписки/реестра останется, но
    вернётся в статус «не разнесён» и будет доступна для повторного
    разнесения.
    """
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    payment = database.db_session.get(Payment, payment_id)
    if payment is None or payment.account_id != account.id:
        abort(404)
    if payment.offset_charge_id is not None:
        # Платёж — одна из двух половин зачёта между счетами (см.
        # transfer_member_account_funds); обычное удаление стёрло бы только
        # эту половину, оставив начисление на счёте-источнике висеть без
        # соответствующего платежа — деньги "терялись" бы для владельца
        # того счёта. Нужна cancel_transfer, которая отменяет обе половины.
        flash(_("Этот платёж — часть зачёта между счетами. Используйте «Отменить зачёт»."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))

    audit.record(
        "payment.delete", entity_type="member_account", entity_id=account.id,
        summary=f"Удалён платёж {audit.format_amount(payment.amount)} от {audit.format_date(payment.date)} "
                f"на счёте {account.account_number} ({account.person.short_name})",
    )
    database.db_session.delete(payment)
    database.db_session.flush()
    reallocate_member_charges(account)
    database.db_session.commit()
    flash(_("Платёж удалён."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/<int:account_id>/payments/<int:payment_id>/cancel-transfer", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def cancel_transfer(account_id, payment_id):
    """
    Отменяет зачёт между счетами целиком — удаляет и платёж (эта функция
    вызывается со счёта-получателя), и парное начисление на счёте-
    источнике (Payment.offset_charge_id, см. transfer_member_account_funds
    и её докстринг), пересчитывая FIFO-разнесение на обоих счетах. Именно
    поэтому это отдельное действие, а не обычное «Удалить платёж» —
    удаление только одной половины оставило бы деньги фактически
    списанными у владельца счёта-источника без следа.
    """
    target = database.db_session.get(MemberAccount, account_id)
    if target is None:
        abort(404)
    payment = database.db_session.get(Payment, payment_id)
    if payment is None or payment.account_id != target.id:
        abort(404)
    if payment.offset_charge_id is None:
        flash(_("Этот платёж не является частью зачёта между счетами."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=target.id))

    charge = database.db_session.get(Charge, payment.offset_charge_id)
    source = database.db_session.get(MemberAccount, charge.account_id) if charge else None

    audit.record(
        "member_account.transfer_cancel", entity_type="member_account", entity_id=target.id,
        summary=(
            f"Отменён зачёт {audit.format_amount(payment.amount)} на счёт {target.account_number} "
            f"({target.person.short_name})"
            + (f" со счёта {source.account_number} ({source.person.short_name})" if source else "")
        ),
    )
    database.db_session.delete(payment)
    if charge is not None:
        database.db_session.delete(charge)
    database.db_session.flush()
    reallocate_member_charges(target)
    if source is not None:
        reallocate_member_charges(source)
    database.db_session.commit()
    flash(_("Зачёт отменён — обе стороны возвращены к исходному состоянию."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=target.id))


# ---------------------------------------------------------------------------
# Зачёт средств между лицевыми счетами — для исправления ошибочного
# разнесения платежа (зачислили не на тот вид взноса) или когда один
# человек по факту заплатил за другого. Разрешено только между счетами
# ОДНОГО И ТОГО ЖЕ человека или ОДНОГО И ТОГО ЖЕ гаража — не между
# произвольными людьми, чтобы кнопкой нельзя было случайно перевести
# деньги постороннему. Уровень доступа — is_board(), как у «Начислить»/
# «Зарегистрировать платёж» (не is_privileged(), как у списания пени: тут
# кооператив ничего не теряет, просто исправляется бухгалтерская запись).
# ---------------------------------------------------------------------------

@bp.route("/member-accounts/<int:account_id>/transfer", methods=["POST"])
@login_required
def transfer_member_account_funds(account_id):
    source = database.db_session.get(MemberAccount, account_id)
    if source is None:
        abort(404)
    if not is_board():
        abort(403)

    f = request.form
    try:
        target_id = int(f["target_account_id"])
        amount = parse_decimal(f["amount"])
    except (KeyError, ValueError, InvalidOperation):
        flash(_("Проверьте выбранный счёт и сумму."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=source.id))
    if amount <= 0:
        flash(_("Сумма зачёта должна быть больше нуля."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=source.id))

    target = database.db_session.get(MemberAccount, target_id)
    if target is None or target.id == source.id:
        flash(_("Выберите другой счёт для зачёта."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=source.id))
    if target.person_id != source.person_id and target.garage_id != source.garage_id:
        flash(_("Зачёт возможен только между счетами одного человека или одного гаража."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=source.id))

    reason = (f.get("reason") or "").strip()
    today = dt.date.today()
    if reason:
        source_comment = _("Зачёт на счёт {number} ({name}): {reason}").format(
            number=target.account_number, name=target.person.short_name, reason=reason)
        target_comment = _("Зачёт со счёта {number} ({name}): {reason}").format(
            number=source.account_number, name=source.person.short_name, reason=reason)
    else:
        source_comment = _("Зачёт на счёт {number} ({name})").format(
            number=target.account_number, name=target.person.short_name)
        target_comment = _("Зачёт со счёта {number} ({name})").format(
            number=source.account_number, name=source.person.short_name)

    source_charge = Charge(
        account_id=source.id, year=today.year, amount=amount,
        related_person_id=target.person_id, comment=source_comment,
    )
    database.db_session.add(source_charge)
    database.db_session.flush()  # нужен source_charge.id для offset_charge_id ниже
    database.db_session.add(Payment(
        account_id=target.id, date=today, amount=amount,
        related_person_id=source.person_id, comment=target_comment,
        offset_charge_id=source_charge.id,
    ))
    database.db_session.flush()
    reallocate_member_charges(source)
    reallocate_member_charges(target)
    audit.record(
        "member_account.transfer", entity_type="member_account", entity_id=source.id,
        summary=(
            f"Зачёт {audit.format_amount(amount)} со счёта {source.account_number} ({source.person.short_name}) "
            f"на счёт {target.account_number} ({target.person.short_name})"
        ) + (f": {reason}" if reason else ""),
    )
    database.db_session.commit()
    flash(_("Зачёт выполнен."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=source.id))


# ---------------------------------------------------------------------------
# Списание пени — для случаев, когда кооператив отказывается от взыскания
# (мировое соглашение, добровольный отказ от претензий и т.п.). Только
# председатель и бухгалтер (is_privileged() — не is_board(): рядовому члену
# правления прощать долги не положено); только для счетов вида взноса
# «пеня» — списание обычных взносов/налога этой кнопкой не делается,
# отдельный сценарий, не то, о чём просили.
# ---------------------------------------------------------------------------

def _write_off_penalty_account(account: MemberAccount, reason: str) -> Decimal | None:
    """Погашающий платёж на всю непогашенную пеню счёта — общая часть между
    write_off_penalty (один счёт, со страницы счёта) и
    write_off_person_penalties (сразу все пенные счета человека, со страницы
    персоны). Не коммитит — вызывающий делает это сам, обычно после цикла по
    нескольким счетам. Возвращает списанную сумму или None, если счёт не
    «пеня» либо непогашенной пени на нём нет (вызывающий сам решает, что
    в этом случае показать пользователю)."""
    if not account.fee_type.is_penalty:
        return None
    current_balance = _balance(account)
    if current_balance >= 0:
        return None

    amount = -current_balance
    database.db_session.add(Payment(
        account_id=account.id, date=dt.date.today(), amount=amount,
        comment=_("Списание пени: {reason}").format(reason=reason),
    ))
    database.db_session.flush()
    reallocate_member_charges(account)
    audit.record(
        "penalty.write_off", entity_type="member_account", entity_id=account.id,
        summary=f"Списана пеня {audit.format_amount(amount)} на счёте {account.account_number} "
                f"({account.person.short_name}): {reason}",
    )
    return amount


@bp.route("/member-accounts/<int:account_id>/write-off-penalty", methods=["POST"])
@login_required
def write_off_penalty(account_id):
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    if not is_privileged():
        abort(403)
    if not account.fee_type.is_penalty:
        flash(_("Списание доступно только для счетов пени."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash(_("Укажите причину списания (мировое соглашение, отказ от взыскания и т.п.)."), "danger")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))

    amount = _write_off_penalty_account(account, reason)
    if amount is None:
        flash(_("По этому счёту нет непогашенной пени — списывать нечего."), "warning")
        return redirect(url_for("finance.member_account_detail", account_id=account.id))

    database.db_session.commit()
    flash(_("Пеня списана."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


# ---------------------------------------------------------------------------
# Те же два действия (списать/удалить пеню), но сразу по НЕСКОЛЬКИМ пенным
# счетам одним нажатием — кнопка «Пеня» рядом с таблицей, а не по одному на
# каждой карточке счёта. Два места, где она есть: карточка персоны
# (persons/detail.html — только счета этого человека) и общий список
# лицевых счетов (finance/member_accounts.html — вообще все пенные счета
# кооператива, самая широкая версия действия). Логика/уровни доступа
# совпадают с одиночными действиями выше (write_off_penalty) и с
# delete_member_charge — просто применены в цикле; общее ядро цикла
# вынесено в _write_off_penalties_bulk/_delete_penalties_bulk ниже, чтобы
# не дублировать между «по одному человеку» и «по всем счетам».
# ---------------------------------------------------------------------------

def _penalty_accounts_query():
    return (
        database.db_session.query(MemberAccount)
        .join(FeeType, MemberAccount.fee_type_id == FeeType.id)
        .filter(FeeType.is_penalty.is_(True))
    )


def _write_off_penalties_bulk(accounts: list[MemberAccount], reason: str) -> tuple[int, Decimal]:
    """Гасит непогашенную пеню по каждому счёту из accounts (см.
    _write_off_penalty_account), не коммитит — вызывающий коммитит сам.
    Возвращает (число счетов, на которые реально что-то списали, суммарно
    списанное)."""
    total = Decimal("0")
    written_off = 0
    for account in accounts:
        amount = _write_off_penalty_account(account, reason)
        if amount is not None:
            total += amount
            written_off += 1
    return written_off, total


def _delete_penalties_bulk(accounts: list[MemberAccount]) -> tuple[int, int]:
    """Удаляет все начисления пени по каждому счёту из accounts, не коммитит.
    Возвращает (удалено начислений, пропущено — начисление оказалось
    источником зачёта между счетами, см. delete_member_charge)."""
    deleted = 0
    skipped = 0
    for account in accounts:
        charges = list(account.charges)
        if not charges:
            continue
        touched = False
        for charge in charges:
            # Как и в delete_member_charge — начисление-источник зачёта между
            # счетами трогать нельзя, связь рвётся только через
            # «Отменить зачёт» на платеже. Для пенных счетов это в
            # действительности не встречается (зачёт заводится между
            # обычными счетами), но проверка на всякий случай — дешёвая и
            # предотвращает молчаливую порчу данных, если это когда-то
            # изменится.
            if database.db_session.query(Payment).filter_by(offset_charge_id=charge.id).first() is not None:
                skipped += 1
                continue
            database.db_session.delete(charge)
            deleted += 1
            touched = True
        if touched:
            database.db_session.flush()
            # account.charges уже был загружен в память строкой выше
            # (charges = list(account.charges)) — сам по себе flush() не
            # обновляет эту закешированную коллекцию, она по-прежнему
            # содержит только что удалённые объекты. reallocate_member_charges
            # читает именно account.charges — без expire() он попытался бы
            # разнести платежи по уже удалённым начислениям и упал бы на
            # внешнем ключе (IntegrityError на INSERT INTO charge_allocation).
            database.db_session.expire(account, ["charges"])
            reallocate_member_charges(account)
    return deleted, skipped


@bp.route("/persons/<int:person_id>/write-off-penalties", methods=["POST"])
@login_required
def write_off_person_penalties(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    if not is_privileged():
        abort(403)

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash(_("Укажите причину списания (мировое соглашение, отказ от взыскания и т.п.)."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

    accounts = _penalty_accounts_query().filter(MemberAccount.person_id == person.id).all()
    written_off, total = _write_off_penalties_bulk(accounts, reason)
    if written_off == 0:
        flash(_("Непогашенной пени не найдено — списывать нечего."), "warning")
        return redirect(url_for("persons.detail", person_id=person.id))

    database.db_session.commit()
    flash(_("Списана пеня по {n} счетам на сумму {total} ₽.", n=written_off, total=total), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/persons/<int:person_id>/delete-penalties", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_person_penalties(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)

    accounts = _penalty_accounts_query().filter(MemberAccount.person_id == person.id).all()
    deleted, skipped = _delete_penalties_bulk(accounts)
    if deleted == 0:
        flash(_("Начисленной пени не найдено — удалять нечего."), "warning")
        return redirect(url_for("persons.detail", person_id=person.id))

    audit.record(
        "penalty.delete_all", entity_type="person", entity_id=person.id,
        summary=f"Удалено начислений пени: {deleted} — {person.short_name}",
    )
    database.db_session.commit()
    msg = _("Удалено начислений пени: {n}.", n=deleted)
    if skipped:
        msg += " " + _("Пропущено (часть зачёта между счетами): {n}.", n=skipped)
    flash(msg, "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/member-accounts/write-off-penalties", methods=["POST"])
@login_required
def write_off_all_penalties():
    """Как write_off_person_penalties, но без фильтра по человеку — сразу по
    ВСЕМ пенным счетам кооператива. Кнопка на общем списке лицевых счетов
    (finance/member_accounts.html)."""
    if not is_privileged():
        abort(403)

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash(_("Укажите причину списания (мировое соглашение, отказ от взыскания и т.п.)."), "danger")
        return redirect(url_for("finance.member_accounts"))

    accounts = _penalty_accounts_query().all()
    written_off, total = _write_off_penalties_bulk(accounts, reason)
    if written_off == 0:
        flash(_("Непогашенной пени не найдено — списывать нечего."), "warning")
        return redirect(url_for("finance.member_accounts"))

    database.db_session.commit()
    flash(_("Списана пеня по {n} счетам на сумму {total} ₽.", n=written_off, total=total), "success")
    return redirect(url_for("finance.member_accounts"))


@bp.route("/member-accounts/delete-penalties", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_all_penalties():
    """Как delete_person_penalties, но без фильтра по человеку — сразу по ВСЕМ
    пенным счетам кооператива."""
    accounts = _penalty_accounts_query().all()
    deleted, skipped = _delete_penalties_bulk(accounts)
    if deleted == 0:
        flash(_("Начисленной пени не найдено — удалять нечего."), "warning")
        return redirect(url_for("finance.member_accounts"))

    audit.record(
        "penalty.delete_all", entity_type="cooperative",
        summary=f"Удалено начислений пени (по всем счетам кооператива): {deleted}",
    )
    database.db_session.commit()
    msg = _("Удалено начислений пени: {n}.", n=deleted)
    if skipped:
        msg += " " + _("Пропущено (часть зачёта между счетами): {n}.", n=skipped)
    flash(msg, "success")
    return redirect(url_for("finance.member_accounts"))


@bp.route("/member-accounts/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def create_member_account():
    f = request.form
    person_id = int(f["person_id"])
    garage_id = int(f["garage_id"])
    fee_type_id = int(f["fee_type_id"])

    existing = database.db_session.query(MemberAccount).filter_by(
        person_id=person_id, garage_id=garage_id, fee_type_id=fee_type_id, is_archived=False,
    ).first()
    if existing:
        flash(_("Такой счёт уже существует."), "warning")
        return redirect(url_for("finance.member_account_detail", account_id=existing.id))

    account_number = (f.get("account_number") or "").strip()
    if not account_number:
        garage = database.db_session.get(Garage, garage_id)
        fee_type = database.db_session.get(FeeType, fee_type_id)
        if not fee_type.type_code:
            flash(_("У этого вида взноса нет кода счёта — укажите номер счёта вручную."), "danger")
            return redirect(url_for("finance.member_accounts"))
        owner_index = owner_index_for(garage_id, person_id)
        account_number = member_account_number(fee_type.type_code, garage.id, owner_index, fee_type.is_penalty)

    account = MemberAccount(
        person_id=person_id, garage_id=garage_id, fee_type_id=fee_type_id, account_number=account_number,
    )
    database.db_session.add(account)
    try:
        database.db_session.commit()
    except Exception:
        database.db_session.rollback()
        flash(_("Такой номер счёта уже используется."), "danger")
        return redirect(url_for("finance.member_accounts"))
    flash(_("Счёт создан."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/<int:account_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_member_account(account_id):
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    audit.record(
        "member_account.delete", entity_type="member_account", entity_id=account.id,
        summary=f"Удалён счёт {account.account_number} ({account.person.short_name}, "
                f"гараж {account.garage.number}, {account.fee_type.name})",
    )
    database.db_session.delete(account)
    database.db_session.commit()
    flash(_("Счёт удалён."), "success")
    return redirect(url_for("finance.member_accounts"))


# ---------------------------------------------------------------------------
# Формат номеров лицевых счетов
# ---------------------------------------------------------------------------

def _regenerate_account_numbers(settings) -> tuple[int, int]:
    """
    Пересчитывает номера всех существующих счетов под новые настройки формата.
    Меняет только те, что реально отличаются, и только если новый номер
    не конфликтует с уже занятым. Возвращает (изменено, не удалось из-за конфликта).
    """
    changed = 0
    failed = 0

    for account in database.db_session.query(PersonalAccount).join(Garage).all():
        new_number = electricity_account_number(account.garage.id, settings)
        if new_number == account.account_number:
            continue
        conflict = database.db_session.query(PersonalAccount).filter(
            PersonalAccount.account_number == new_number, PersonalAccount.id != account.id
        ).first()
        if conflict:
            failed += 1
            continue
        account.account_number = new_number
        changed += 1

    # индекс собственника по каждому гаражу (порядок по id владения) — нужен для номера счёта члена
    owner_index_by_garage_person = {}
    for garage in database.db_session.query(Garage).all():
        ownerships = (
            database.db_session.query(GarageOwnership)
            .filter_by(garage_id=garage.id)
            .order_by(GarageOwnership.id)
            .all()
        )
        for idx, o in enumerate(ownerships):
            owner_index_by_garage_person[(garage.id, o.person_id)] = idx

    for account in database.db_session.query(MemberAccount).all():
        if not account.fee_type.type_code:
            continue  # у ручных счетов без кода вида — номер не трогаем
        owner_index = owner_index_by_garage_person.get((account.garage_id, account.person_id), 0)
        new_number = member_account_number(
            account.fee_type.type_code, account.garage_id, owner_index, account.fee_type.is_penalty, settings,
        )
        if new_number == account.account_number:
            continue
        conflict = database.db_session.query(MemberAccount).filter(
            MemberAccount.account_number == new_number, MemberAccount.id != account.id
        ).first()
        if conflict:
            failed += 1
            continue
        account.account_number = new_number
        changed += 1

    return changed, failed


@bp.route("/account-format", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def account_format():
    settings = get_settings()

    if request.method == "POST":
        f = request.form
        settings.garage_digits = max(1, min(9, int(f.get("garage_digits") or 3)))
        settings.owner_digits = max(1, min(3, int(f.get("owner_digits") or 1)))
        settings.electricity_prefix = f.get("electricity_prefix", "0")
        settings.penalty_prefix = f.get("penalty_prefix", "П")
        database.db_session.flush()

        if f.get("regenerate_existing"):
            changed, failed = _regenerate_account_numbers(settings)
            database.db_session.commit()
            if failed:
                flash(_(
                    "Формат обновлён. Приведено к новому формату: {changed}. Не удалось из-за конфликта номеров: {failed} — поправьте их вручную на страницах счетов.",
                    changed=changed, failed=failed,
                ), "warning")
            else:
                flash(_("Формат обновлён, все существующие номера приведены к нему. Изменено: {changed}.", changed=changed), "success")
        else:
            # Уже выданные номера намеренно не трогаем — например, при
            # расширении ширины номера собственника (owner_digits), чтобы
            # не упереться в лимит по гаражу с частой сменой собственников
            # (см. accounting.next_owner_index), но без переоформления уже
            # розданных людям счетов. Новый формат применяется только к
            # счетам, которые будут созданы впредь.
            database.db_session.commit()
            flash(_("Формат обновлён. Уже существующие номера оставлены как есть — новый формат применяется только к новым счетам."), "success")
        return redirect(url_for("finance.account_format"))

    return render_template(
        "finance/account_format.html",
        settings=settings,
        example_electricity=electricity_account_number(95, settings),
        example_member=member_account_number("1", 95, 0, False, settings),
        example_penalty=member_account_number("1", 95, 0, True, settings),
    )


# ---------------------------------------------------------------------------
# Массовое начисление на лицевые счета членов кооператива
# ---------------------------------------------------------------------------

@bp.route("/mass-charge", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def mass_charge():
    """
    Массовое начисление на лицевые счета членов кооператива, с расчётом
    суммы по одной из трёх стратегий (по коэффициенту гаража / по площади
    от общей суммы / земельный налог). Раньше выбор конкретных гаражей жил
    отдельной страницей (garages.add_charge_page, «Начисления на гаражи»,
    с ручной фиксированной суммой без стратегий расчёта) — по факту
    дублировала эту страницу, но без вариантов расчёта. Объединили: выбор
    гаражей теперь прямо здесь (необязательный — если ничего не отмечено,
    начисление идёт на все гаражи, как и раньше).
    """
    fee_types_list = database.db_session.query(FeeType).filter(
        FeeType.type_code.isnot(None), FeeType.is_penalty.is_(False)
    ).order_by(FeeType.name).all()
    coop = database.db_session.query(Cooperative).first()
    all_garages = database.db_session.query(Garage).order_by(Garage.number).all()
    person_names = {
        garage.id: ", ".join(o.person.full_name for o in garage.ownerships)
        for garage in all_garages
    }
    results = None

    if request.method == "POST":
        f = request.form
        year = int(f["year"])
        strategy = f["strategy"]

        selected_ids = [int(x) for x in f.getlist("garage_id")]
        garages = [g for g in all_garages if g.id in set(selected_ids)] if selected_ids else all_garages
        total_area = sum((garage.area_sqm for garage in garages), Decimal("0"))

        charged_rows = []   # (person_name, garage_number, amount)
        skipped_rows = []   # (person_name, garage_number) — нет лицевого счёта на этот вид взноса

        round_up_raw = f.get("round_up", "0")
        round_up = int(round_up_raw) if round_up_raw else 0

        if strategy == "land_tax":
            fee_type = database.db_session.query(FeeType).filter_by(code="land_tax").first()
            if fee_type is None:
                flash(_("Не найден вид взноса «land_tax» — проверьте справочник видов взносов."), "danger")
                return render_template("finance/mass_charge.html", fee_types=fee_types_list, results=None, coop=coop, garages=all_garages, person_names=person_names)

            garage_amounts = compute_land_tax(year)
            if garage_amounts is None:
                flash(_(
                    "Недостаточно данных для расчёта: заполните текущую площадь и кадастровую стоимость кооператива в его карточке.",
                ), "danger")
                return render_template("finance/mass_charge.html", fee_types=fee_types_list, results=None, coop=coop, garages=all_garages, person_names=person_names)
        elif strategy == "coefficient":
            fee_type_id = int(f["fee_type_id"])
            fee_type = database.db_session.get(FeeType, fee_type_id)
            base_amount = parse_decimal(f["base_amount"])
            if fee_type is not None and fee_type.code == "land_tax":
                # Земельный налог начисляется по-разному в зависимости от
                # того, приватизирован ли участок под конкретным гаражом
                # (см. compute_land_tax — та же логика для автоматической
                # стратегии «land_tax» ниже) — поэтому для этого вида взноса
                # форма даёт вторую сумму, для гаражей с приватизированной
                # землёй, а не одну общую на всех.
                base_amount_privatized = parse_decimal(f.get("base_amount_privatized") or "0")
                garage_amounts = {
                    garage.id: (base_amount_privatized if garage.land_privatized else base_amount) * garage.coefficient
                    for garage in garages
                }
            else:
                garage_amounts = {garage.id: base_amount * garage.coefficient for garage in garages}
        else:  # "total_area"
            fee_type_id = int(f["fee_type_id"])
            fee_type = database.db_session.get(FeeType, fee_type_id)
            total_amount = parse_decimal(f["total_amount"])
            if total_area > 0:
                garage_amounts = {garage.id: total_amount * (garage.area_sqm / total_area) for garage in garages}
            else:
                garage_amounts = {garage.id: Decimal("0") for garage in garages}

        def _round_up(value, step):
            if step <= 0:
                return value.quantize(Decimal("0.01"))
            int_value = int(value)
            remainder = int_value % step
            if remainder == 0:
                return Decimal(int_value).quantize(Decimal("0.01"))
            return Decimal(int_value + step - remainder).quantize(Decimal("0.01"))

        for garage in garages:
            garage_amount = garage_amounts[garage.id]
            for ownership in garage.ownerships:
                account = database.db_session.query(MemberAccount).filter_by(
                    person_id=ownership.person_id, garage_id=garage.id, fee_type_id=fee_type.id,
                ).first()
                owner_amount = (garage_amount * ownership.share).quantize(Decimal("0.01"))
                if account is None:
                    skipped_rows.append((ownership.person.full_name, garage.number))
                    continue
                owner_amount = _round_up(owner_amount, round_up)
                database.db_session.add(Charge(
                    account_id=account.id,
                    fee_type_id=fee_type.id,
                    year=year,
                    amount=owner_amount,
                    comment=f"Массовое начисление за {year} год",
                ))
                charged_rows.append((ownership.person.full_name, garage.number, owner_amount))

        database.db_session.flush()
        touched_accounts = {
            (ownership.person_id, garage.id, fee_type.id)
            for garage in garages for ownership in garage.ownerships
        }
        if touched_accounts:
            accounts = (
                database.db_session.query(MemberAccount)
                .filter(MemberAccount.fee_type_id == fee_type.id)
                .all()
            )
            for acc in accounts:
                if (acc.person_id, acc.garage_id, acc.fee_type_id) in touched_accounts:
                    reallocate_member_charges(acc)
        if charged_rows:
            audit.record(
                "charge.mass_create", entity_type="fee_type", entity_id=fee_type.id,
                summary=f"Массовое начисление «{fee_type.name}» за {year} год: {len(charged_rows)} счетов "
                        f"на сумму {audit.format_amount(sum((a for _n, _g, a in charged_rows), Decimal('0')))}",
            )
        database.db_session.commit()
        results = {
            "fee_type_name": fee_type.name,
            "year": year,
            "charged_rows": charged_rows,
            "skipped_rows": skipped_rows,
            "total": sum((amount for _n, _g, amount in charged_rows), Decimal("0")),
        }
        if charged_rows:
            flash(_("Начислено счетов: {n}.", n=len(charged_rows)), "success")
        if skipped_rows:
            flash(_(
                "Пропущено (нет лицевого счёта на этот вид взноса): {n}. Счета заводятся автоматически при добавлении собственника — проверьте вид взноса и код счёта.",
                n=len(skipped_rows),
            ), "warning")

    return render_template("finance/mass_charge.html", fee_types=fee_types_list, results=results, coop=coop, garages=all_garages, person_names=person_names)
