import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .permissions import can_view_member_account, is_board
from .models import (
    GarageOwnership, Charge, Payment, Garage, PersonalAccount,
    FeeType, MemberAccount, Person, RoleEnum,
    Cooperative, LandTaxYear,
)
from .accounting import get_settings, electricity_account_number, member_account_number, balance as _balance, compute_land_tax

bp = Blueprint("finance", __name__, url_prefix="/finance")


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
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    all_garages = database.db_session.query(Garage).order_by(Garage.number).all()
    all_fee_types = database.db_session.query(FeeType).order_by(FeeType.name).all()
    return render_template(
        "finance/member_accounts.html", rows=rows,
        all_persons=all_persons, all_garages=all_garages, all_fee_types=all_fee_types,
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
    return render_template("finance/member_account_detail.html", account=account, balance=_balance(account))


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


@bp.route("/member-accounts/<int:account_id>/charges/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_member_charge(account_id):
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    f = request.form
    database.db_session.add(Charge(
        account_id=account.id, year=int(f["year"]), amount=Decimal(f["amount"]), comment=f.get("comment") or None,
    ))
    database.db_session.commit()
    flash(_("Начисление добавлено."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/<int:account_id>/payments/add", methods=["POST"])
@login_required
def add_member_payment(account_id):
    account = database.db_session.get(MemberAccount, account_id)
    if account is None:
        abort(404)
    if not is_board():
        abort(403)
    f = request.form
    database.db_session.add(Payment(
        account_id=account.id,
        date=dt.date.fromisoformat(f["date"]),
        amount=Decimal(f["amount"]),
        comment=f.get("comment") or None,
    ))
    database.db_session.commit()
    flash(_("Платёж зарегистрирован."), "success")
    return redirect(url_for("finance.member_account_detail", account_id=account.id))


@bp.route("/member-accounts/new", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def create_member_account():
    f = request.form
    person_id = int(f["person_id"])
    garage_id = int(f["garage_id"])
    fee_type_id = int(f["fee_type_id"])

    existing = database.db_session.query(MemberAccount).filter_by(
        person_id=person_id, garage_id=garage_id, fee_type_id=fee_type_id
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
        ownerships = (
            database.db_session.query(GarageOwnership)
            .filter_by(garage_id=garage_id)
            .order_by(GarageOwnership.id)
            .all()
        )
        owner_index = next((i for i, o in enumerate(ownerships) if o.person_id == person_id), 0)
        account_number = member_account_number(fee_type.type_code, garage.number, owner_index, fee_type.is_penalty)

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
        new_number = electricity_account_number(account.garage.number, settings)
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
            account.fee_type.type_code, account.garage.number, owner_index, account.fee_type.is_penalty, settings,
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

        changed, failed = _regenerate_account_numbers(settings)
        database.db_session.commit()

        if failed:
            flash(_(
                "Формат обновлён. Приведено к новому формату: {changed}. Не удалось из-за конфликта номеров: {failed} — поправьте их вручную на страницах счетов.",
                changed=changed, failed=failed,
            ), "warning")
        else:
            flash(_("Формат обновлён, все существующие номера приведены к нему. Изменено: {changed}.", changed=changed), "success")
        return redirect(url_for("finance.account_format"))

    return render_template(
        "finance/account_format.html",
        settings=settings,
        example_electricity=electricity_account_number("95", settings),
        example_member=member_account_number("1", "95", 0, False, settings),
        example_penalty=member_account_number("1", "95", 0, True, settings),
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
    fee_types_list = database.db_session.query(FeeType).filter(FeeType.type_code.isnot(None)).order_by(FeeType.name).all()
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

        if strategy == "land_tax":
            fee_type = database.db_session.query(FeeType).filter_by(code="land_tax").first()
            if fee_type is None:
                flash(_("Не найден вид взноса «land_tax» — проверьте справочник видов взносов."), "danger")
                return render_template("finance/mass_charge.html", fee_types=fee_types_list, results=None, coop=coop, garages=all_garages, person_names=person_names)

            cadastral_value_raw = f.get("cadastral_value")
            if cadastral_value_raw:
                land_tax_year = database.db_session.query(LandTaxYear).filter_by(year=year).first()
                if land_tax_year is None:
                    land_tax_year = LandTaxYear(year=year, cadastral_value=Decimal(cadastral_value_raw))
                    database.db_session.add(land_tax_year)
                else:
                    land_tax_year.cadastral_value = Decimal(cadastral_value_raw)
                database.db_session.flush()

            garage_amounts = compute_land_tax(year)
            if garage_amounts is None:
                flash(_(
                    "Недостаточно данных для расчёта: заполните площади кооператива в его карточке и кадастровую стоимость на {year} год.", year=year,
                ), "danger")
                return render_template("finance/mass_charge.html", fee_types=fee_types_list, results=None, coop=coop, garages=all_garages, person_names=person_names)
        elif strategy == "coefficient":
            fee_type_id = int(f["fee_type_id"])
            fee_type = database.db_session.get(FeeType, fee_type_id)
            base_amount = Decimal(f["base_amount"])
            garage_amounts = {garage.id: base_amount * garage.coefficient for garage in garages}
        else:  # "total_area"
            fee_type_id = int(f["fee_type_id"])
            fee_type = database.db_session.get(FeeType, fee_type_id)
            total_amount = Decimal(f["total_amount"])
            if total_area > 0:
                garage_amounts = {garage.id: total_amount * (garage.area_sqm / total_area) for garage in garages}
            else:
                garage_amounts = {garage.id: Decimal("0") for garage in garages}

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
                database.db_session.add(Charge(account_id=account.id, year=year, amount=owner_amount))
                charged_rows.append((ownership.person.full_name, garage.number, owner_amount))

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
