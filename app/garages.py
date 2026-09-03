import datetime as dt
import os
import uuid
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, g, abort,
    current_app, send_from_directory,
)

from . import database
from . import audit
from .i18n import translate as _, parse_decimal
from .auth import login_required, roles_required
from .permissions import is_board, is_owner_or_board, is_chairman, is_privileged, can_view_member_account
from .models import (
    Garage, Person, GarageOwnership, GarageOwnershipEvent, GarageOwnershipEventType, GarageContact, GaragePhoto,
    PersonalAccount, MemberAccount, FeeType, RoleEnum, ElectricityMeter, ElectricityReading,
    Charge, Payment, User,
)
from sqlalchemy.orm import joinedload
from .accounting import (
    electricity_account_number, member_account_number, balance, current_tariff, reallocate_garage_charges,
    charge_paid_amount, split_amount_by_shares, redistribute_member_account_balance, next_owner_index,
    transfer_member_account_balance,
)

bp = Blueprint("garages", __name__, url_prefix="/garages")


def _current_meter(garage: Garage):
    """Актуальный счётчик — последняя по дате установки запись."""
    if not garage.meters:
        return None
    return sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id))[-1]


def _meter_history(garage: Garage):
    """Все счётчики, отсортированные от новых к старым."""
    return sorted(garage.meters, key=lambda m: (m.installed_date or dt.date.min, m.id), reverse=True)


def _ensure_member_accounts(garage: Garage, person_id: int, owner_index: int):
    """
    Заводит члену кооператива лицевые счета на все виды взносов/налогов,
    для которых задан type_code (см. FeeType), по этому гаражу — если их
    ещё нет. Электричество сюда не входит — у него отдельный счёт на гараж.
    """
    fee_types = (
        database.db_session.query(FeeType)
        .filter(FeeType.type_code.isnot(None))
        .all()
    )
    for fee_type in fee_types:
        exists = (
            database.db_session.query(MemberAccount)
            .filter_by(person_id=person_id, garage_id=garage.id, fee_type_id=fee_type.id, is_archived=False)
            .first()
        )
        if exists:
            continue
        number = member_account_number(fee_type.type_code, garage.id, owner_index, fee_type.is_penalty)
        database.db_session.add(MemberAccount(
            person_id=person_id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=number,
        ))


def _archive_owner_accounts_and_reuse(garage: Garage, new_person_id: int) -> None:
    """
    Гараж перед этим остался вовсе без собственников (последний выбыл —
    см. _remove_owner_and_redistribute, где для этого случая лицевые счета
    сознательно не трогаются), а теперь у него появился новый собственник.

    Вместо того чтобы завести ему счета с новыми номерами
    (next_owner_index — так было бы для «просто нового» совладельца),
    переиспользуем номера прежних счетов: старые счета помечаются
    is_archived=True (остаются привязаны к прежнему person_id — история
    начислений/платежей никуда не девается), новому собственнику заводятся
    свежие счета С ТЕМИ ЖЕ номерами (см. частичный уникальный индекс на
    MemberAccount.account_number — совпадение номера у активного и
    архивного счёта не конфликт, у двух активных — конфликт), а остаток
    (долг/переплата) переносится на новый счёт (accounting.
    transfer_member_account_balance) — не откладывается до отдельного
    решения правления, как было при выбытии без замены.

    Виды взносов, на которые у гаража почему-то ещё не было счёта (не
    должно случаться в норме — у единственного собственника счета
    заводятся на все fee_types сразу, см. _ensure_member_accounts), на
    всякий случай заводятся как обычно, новым номером.

    Отдельный случай — ТОТ ЖЕ человек, что и выбыл, возвращается в
    собственники (например, ошибочно удалили и тут же добавили обратно):
    его счета всё это время оставались активными (см.
    _remove_owner_and_redistribute — единственному собственнику при
    выбытии ничего не трогаем), архивировать и дублировать их нечем —
    пропускаем, оставляем как есть. Раньше (баг) код всё равно пытался
    завести ему «новый» счёт с тем же номером — UNIQUE constraint failed
    по (person_id, garage_id, fee_type_id), т.к. это буквально его же
    активная запись.
    """
    old_accounts = (
        database.db_session.query(MemberAccount)
        .filter_by(garage_id=garage.id, is_archived=False)
        .all()
    )
    handled_fee_type_ids = set()
    for old_account in old_accounts:
        handled_fee_type_ids.add(old_account.fee_type_id)
        if old_account.person_id == new_person_id:
            continue
        old_account.is_archived = True
        new_account = MemberAccount(
            person_id=new_person_id, garage_id=garage.id, fee_type_id=old_account.fee_type_id,
            account_number=old_account.account_number,
        )
        database.db_session.add(new_account)
        database.db_session.flush()
        transfer_member_account_balance(old_account, new_account)

    fee_types = (
        database.db_session.query(FeeType)
        .filter(FeeType.type_code.isnot(None))
        .all()
    )
    missing_fee_types = [ft for ft in fee_types if ft.id not in handled_fee_type_ids]
    if missing_fee_types:
        owner_index = next_owner_index(garage.id)
        for fee_type in missing_fee_types:
            number = member_account_number(fee_type.type_code, garage.id, owner_index, fee_type.is_penalty)
            database.db_session.add(MemberAccount(
                person_id=new_person_id, garage_id=garage.id, fee_type_id=fee_type.id, account_number=number,
            ))


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_garages():
    garages = (
        database.db_session.query(Garage)
        .options(joinedload(Garage.meters))
        .order_by(Garage.number)
        .all()
    )
    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    preselect_person_id = request.args.get("new_person_id", type=int)
    balances = {garage.id: (balance(garage) if garage else None) for garage in garages}
    # Подсветка строки в списке (см. garages/list.html) — не отдельная
    # колонка, чтобы не занимать место под то, что нужно только иногда.
    has_meter = {garage.id: (_current_meter(garage) is not None) for garage in garages}
    return render_template(
        "garages/list.html", garages=garages, all_persons=all_persons,
        preselect_person_id=preselect_person_id, balances=balances,
        has_meter=has_meter, any_no_meter=(False in has_meter.values()),
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        f = request.form
        garage = Garage(
            number=f["number"],
            area_sqm=parse_decimal(f["area_sqm"]),
            coefficient=parse_decimal(f["coefficient"]) if f.get("coefficient") else Decimal("1"),
            land_privatized=bool(f.get("land_privatized")),
            cadastral_number=f.get("cadastral_number") or None,
            land_cadastral_number=f.get("land_cadastral_number") or None,
            privatized_land_area=parse_decimal(f["privatized_land_area"]) if f.get("privatized_land_area") else None,
            comment=f.get("comment") or None,
        )
        database.db_session.add(garage)
        database.db_session.flush()  # чтобы получить garage.id

        # лицевой счёт на электричество заводится автоматически вместе с гаражом
        account = PersonalAccount(garage_id=garage.id, account_number=electricity_account_number(garage.id))
        database.db_session.add(account)

        # собственники, указанные прямо в форме создания
        person_ids = request.form.getlist("owner_person_id")
        shares = request.form.getlist("owner_share")
        owner_index = 0
        for person_id, share_raw in zip(person_ids, shares):
            if not person_id:
                continue
            try:
                share = parse_decimal(share_raw) if share_raw else Decimal("1")  # пусто -> вся доля (100%)
            except InvalidOperation:
                continue
            if not (0 < share <= 1):
                continue
            database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=int(person_id), share=share))
            database.db_session.flush()
            _ensure_member_accounts(garage, int(person_id), owner_index)
            owner_index += 1

        # фото гаража (необязательно)
        upload = request.files.get("photo")
        if upload and upload.filename:
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext in ALLOWED_PHOTO_EXT:
                stored_name = f"{uuid.uuid4().hex}{ext}"
                upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))
                database.db_session.add(GaragePhoto(garage_id=garage.id, file_path=stored_name))
            else:
                flash(_("Фото не сохранено: поддерживаются только изображения (jpg, png, webp, gif)."), "warning")

        audit.record("garage.create", f"Создан гараж №{garage.number}, лицевой счёт открыт", entity_type="garage", entity_id=garage.id)
        database.db_session.commit()
        flash(_("Гараж №{number} создан, лицевой счёт открыт.", number=garage.number), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id))

    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    preselect_person_id = request.args.get("new_person_id", type=int)
    return render_template(
        "garages/form.html", garage=None, all_persons=all_persons, preselect_person_id=preselect_person_id
    )


def _truncate(text: str, max_len: int = 70) -> str:
    """Обрезаем текст, добавляя «…», если не помещается."""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "\u2026"


@bp.route("/<int:garage_id>")
@login_required
def detail(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages") if is_board() else url_for("cabinet.garages"))
    if not is_owner_or_board(garage):
        abort(403)

    # предыдущий / следующий гараж (по ID)
    prev_garage = (
        database.db_session.query(Garage)
        .filter(Garage.id < garage.id)
        .order_by(Garage.id.desc())
        .first()
    )
    next_garage = (
        database.db_session.query(Garage)
        .filter(Garage.id > garage.id)
        .order_by(Garage.id)
        .first()
    )

    all_persons = database.db_session.query(Person).order_by(Person.full_name).all()
    total_share = sum((o.share for o in garage.ownerships), Decimal("0"))
    preselect_contact_person_id = request.args.get("new_person_id", type=int)

    # электричество
    current_meter = _current_meter(garage)
    meter_history = _meter_history(garage)
    readings = sorted(current_meter.readings, key=lambda r: r.reading_date, reverse=True) if current_meter else []

    # показания всех счётчиков гаража — для журнала показаний и начислений
    # (при смене счётчика старые показания из истории тоже должны быть видны)
    all_readings = (
        database.db_session.query(ElectricityReading)
        .join(ElectricityMeter)
        .filter(ElectricityMeter.garage_id == garage.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .all()
    )

    # электросчёт
    account = garage.account
    acc_balance = balance(account.garage) if account else None
    charges = sorted(account.garage.charges, key=lambda c: c.year, reverse=True) if account else []
    payments = sorted(account.garage.payments, key=lambda p: p.date, reverse=True) if account else []
    fee_types_list = database.db_session.query(FeeType).order_by(FeeType.name).all()

    # Лицевые счета гаража — сводка балансов для вкладки «Информация»: и
    # электричество (одно на гараж), и все MemberAccount (по одному на
    # каждого собственника и вид взноса — взносы/налог зависят от доли
    # владения, поэтому у каждого собственника свой счёт даже на один и
    # тот же гараж). Рядовой собственник видит в этом списке только СВОИ
    # счета (can_view_member_account), не счета содольщиков — их баланс
    # не его дело; правление видит все. Пени без начислений (баланс — 0,
    # как и у полностью погашенных) НЕ отфильтровываются здесь — это дело
    # чекбокса «Актуальные» на клиенте (data-zero-penalty, см.
    # garages/detail.html), иначе для таких счетов чекбокс не работал бы:
    # строка просто не долетала бы до шаблона ни в каком состоянии галки.
    member_accounts = (
        database.db_session.query(MemberAccount)
        .filter_by(garage_id=garage.id)
        .options(joinedload(MemberAccount.charges))
        .all()
    )
    member_accounts.sort(key=lambda ma: (ma.person.full_name, ma.fee_type.name))
    account_summary_rows = [
        {
            "id": ma.id, "person": ma.person, "fee_type": ma.fee_type, "account_number": ma.account_number,
            "balance": balance(ma), "is_archived": ma.is_archived,
        }
        for ma in member_accounts
        if can_view_member_account(ma)
    ]

    # объединённая таблица: показание берёт своё начисление напрямую через связь
    # Charge.reading (FK reading_id, а не текстовым сопоставлением); начисления без
    # привязки к показанию (ручные, любого вида взноса) идут отдельными строками той
    # же таблицы. Платежи, закрывающие начисление (через ChargeAllocation), показаны
    # в столбцах «От кого/Дата оплаты/Сумма» ЭТОЙ ЖЕ строки начисления, а не отдельной
    # строкой — так по одному начислению не возникает лишней строки на каждый платёж.
    # Отдельной строкой платёж идёт только на ту часть суммы, которая не пошла ни на
    # одно начисление (аванс/переплата).
    ledger_rows = []

    def _charge_payments(charge_obj):
        return [{
            "payer": a.payment.payer.full_name if a.payment.payer else None,
            "date": a.payment.date,
            "amount": a.amount,
        } for a in sorted(charge_obj.allocations, key=lambda a: a.payment.date)]

    for r in all_readings:
        charge_obj = r.charge
        ledger_rows.append({
            "sort_date": r.reading_date,
            "reading_date": r.reading_date,
            "reading": r.reading,
            "tariff": r.tariff,
            "charge_amount": charge_obj.amount if charge_obj else None,
            "charge_paid": charge_paid_amount(charge_obj) if charge_obj else None,
            "payments": _charge_payments(charge_obj) if charge_obj else [],
            "meter_number": r.meter.meter_number,
        })
    for c in charges:
        if c.reading_id is not None:
            continue  # уже отражено вместе со своим показанием выше
        ledger_rows.append({
            "sort_date": dt.date(c.year, 1, 1),
            "reading_date": None,
            "reading": None,
            "tariff": None,
            "charge_amount": c.amount,
            "charge_paid": charge_paid_amount(c),
            "charge_label": c.fee_type.name if c.fee_type else None,
            "payments": _charge_payments(c),
        })
    for p in payments:
        allocated = sum((a.amount for a in p.allocations), Decimal("0"))
        unallocated = p.amount - allocated
        if unallocated > 0:
            ledger_rows.append({
                "sort_date": p.date,
                "reading_date": None,
                "reading": None,
                "tariff": None,
                "charge_amount": None,
                "charge_label": _("аванс") if allocated > 0 else None,
                "payments": [{"payer": p.payer.full_name if p.payer else None, "date": p.date, "amount": unallocated}],
            })
    ledger_rows.sort(key=lambda row: row["sort_date"], reverse=True)

    # значения по умолчанию для формы «Зарегистрировать платёж»: сегодняшняя дата,
    # плательщик — тот из собственников, кто платил последним (сортировка списка
    # по дате последнего платежа, от нового к старому), сумма — текущая задолженность
    today = dt.date.today()
    last_payment_by_person = {}
    for p in payments:
        if p.payer_person_id:
            last_payment_by_person[p.payer_person_id] = max(
                last_payment_by_person.get(p.payer_person_id, p.date), p.date
            )
    owners_sorted = sorted(
        garage.ownerships,
        key=lambda o: last_payment_by_person.get(o.person_id, dt.date.min),
        reverse=True,
    )
    default_payer_id = owners_sorted[0].person_id if owners_sorted else None
    default_payment_amount = (
        str((-acc_balance).quantize(Decimal("0.01"))) if acc_balance is not None and acc_balance < 0 else ""
    )

    # заголовок: «Гараж №N (Фамилия И.О.)» — не более 70 символов
    owner_names = ", ".join(
        o.person.short_name for o in garage.ownerships
    )
    title = _("Гараж №{n}", n=garage.number)
    if owner_names:
        title = f"{title} ({owner_names})"
    title = _truncate(title, 70)

    # история собственников — только board видит (та же видимость, что у
    # самой таблицы текущих собственников/её редактирования), см.
    # models.GarageOwnershipEvent: append-only журнал, не источник истины
    # о ТЕКУЩИХ собственниках (для этого по-прежнему garage.ownerships).
    ownership_events = (
        database.db_session.query(GarageOwnershipEvent)
        .filter_by(garage_id=garage.id)
        .options(joinedload(GarageOwnershipEvent.person), joinedload(GarageOwnershipEvent.created_by))
        .order_by(GarageOwnershipEvent.created_at.desc(), GarageOwnershipEvent.id.desc())
        .all()
    ) if is_board() else []

    return render_template(
        "garages/detail.html",
        garage=garage,
        all_persons=all_persons,
        total_share=total_share,
        preselect_contact_person_id=preselect_contact_person_id,
        current_meter=current_meter,
        meter_history=meter_history,
        readings=readings,
        all_readings=all_readings,
        ledger_rows=ledger_rows,
        account=account,
        account_balance=acc_balance,
        account_summary_rows=account_summary_rows,
        charges=charges,
        payments=payments,
        fee_types=fee_types_list,
        today=today,
        owners_sorted=owners_sorted,
        default_payer_id=default_payer_id,
        default_payment_amount=default_payment_amount,
        prev_garage=prev_garage,
        next_garage=next_garage,
        page_title=title,
        ownership_events=ownership_events,
    )


@bp.route("/<int:garage_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages"))

    if request.method == "POST":
        f = request.form
        garage.number = f["number"]
        garage.area_sqm = parse_decimal(f["area_sqm"])
        garage.coefficient = parse_decimal(f["coefficient"]) if f.get("coefficient") else Decimal("1")
        garage.land_privatized = bool(f.get("land_privatized"))
        garage.cadastral_number = f.get("cadastral_number") or None
        garage.land_cadastral_number = f.get("land_cadastral_number") or None
        garage.privatized_land_area = parse_decimal(f["privatized_land_area"]) if f.get("privatized_land_area") else None
        garage.comment = f.get("comment") or None
        audit.record("garage.edit", f"Изменены данные гаража №{garage.number}", entity_type="garage", entity_id=garage.id)
        database.db_session.commit()
        flash(_("Изменения сохранены."), "success")
        return redirect(url_for("garages.detail", garage_id=garage.id))

    return render_template("garages/form.html", garage=garage, all_persons=[], preselect_person_id=None)


@bp.route("/<int:garage_id>/owners/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_owner(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    person_id = int(request.form["person_id"])
    comment = (request.form.get("comment") or "").strip() or None
    try:
        share = parse_decimal(request.form["share"] or "1")
    except InvalidOperation:
        flash(_("Доля должна быть числом (например 0.5)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    if not (0 < share <= 1):
        flash(_("Доля должна быть в диапазоне от 0 (не включая) до 1."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    existing = (
        database.db_session.query(GarageOwnership)
        .filter_by(garage_id=garage.id, person_id=person_id)
        .first()
    )
    if existing:
        existing.share = share
        database.db_session.add(GarageOwnershipEvent(
            garage_id=garage.id, person_id=person_id, event_type=GarageOwnershipEventType.SHARE_CHANGED,
            share=share, comment=comment, created_by_user_id=g.user.id,
        ))
        audit.record(
            "garage_owner.share_change", f"Изменена доля собственника гаража №{garage.number}: {existing.person.full_name} — {share}",
            entity_type="person", entity_id=person_id,
        )
    else:
        # До добавления! После — count() уже включает эту новую запись,
        # проверка утратит смысл (см. _archive_owner_accounts_and_reuse).
        had_no_owners = database.db_session.query(GarageOwnership).filter_by(garage_id=garage.id).count() == 0
        database.db_session.add(GarageOwnership(garage_id=garage.id, person_id=person_id, share=share))
        database.db_session.flush()
        if had_no_owners:
            _archive_owner_accounts_and_reuse(garage, person_id)
        else:
            owner_index = next_owner_index(garage.id)
            _ensure_member_accounts(garage, person_id, owner_index)
        database.db_session.add(GarageOwnershipEvent(
            garage_id=garage.id, person_id=person_id, event_type=GarageOwnershipEventType.ADDED,
            share=share, comment=comment, created_by_user_id=g.user.id,
        ))
        person = database.db_session.get(Person, person_id)
        audit.record(
            "garage_owner.add", f"{person.full_name} добавлен(а) в собственники гаража №{garage.number}, доля {share}",
            entity_type="person", entity_id=person_id,
        )
    database.db_session.commit()
    flash(_("Собственник добавлен/обновлён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/owners/<int:ownership_id>/edit-share", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_owner_share(garage_id, ownership_id):
    ownership = database.db_session.get(GarageOwnership, ownership_id)
    if not ownership or ownership.garage_id != garage_id:
        abort(404)

    try:
        share = parse_decimal(request.form["share"])
    except (InvalidOperation, KeyError):
        flash(_("Доля должна быть числом (например 0.5)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    if not (0 < share <= 1):
        flash(_("Доля должна быть в диапазоне от 0 (не включая) до 1."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    comment = (request.form.get("comment") or "").strip() or None
    ownership.share = share
    database.db_session.add(GarageOwnershipEvent(
        garage_id=garage_id, person_id=ownership.person_id, event_type=GarageOwnershipEventType.SHARE_CHANGED,
        share=share, comment=comment, created_by_user_id=g.user.id,
    ))
    audit.record(
        "garage_owner.share_change", f"Изменена доля собственника гаража №{ownership.garage.number}: {ownership.person.full_name} — {share}",
        entity_type="person", entity_id=ownership.person_id,
    )
    database.db_session.commit()
    flash(_("Доля обновлена."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


def _remove_owner_and_redistribute(garage: Garage, ownership: GarageOwnership, reason: str | None, user_id: int) -> None:
    """
    Общее ядро удаления собственника — используется и при ручном удалении
    с карточки гаража (remove_owner), и при архивации человека
    (persons.archive_person), чтобы поведение не расходилось в двух
    местах (см. явный запрос — синхронизация архивации с выбытием из
    собственников).

    Раньше (до этой правки) удаление собственника сразу же УДАЛЯЛО его
    лицевые счета по этому гаражу — вместе с историей начислений/платежей.
    Теперь счета не удаляются никогда — только обнуляются переносом
    остатка (если были другие собственники, см. ниже) — история остаётся
    доступной для справки («мало ли какие вопросы»), а при повторном
    появлении этого же человека как собственника _ensure_member_accounts
    сам найдёт и переиспользует существующий счёт, не заведёт дубль.

    Если были ДРУГИЕ собственники: их доли пересчитываются пропорционально
    друг другу (чтобы снова суммировались в 1 — «доля оставшихся»), и
    остаток лицевых счетов выбывшего по этому гаражу распределяется между
    ними в этой же пропорции (см. accounting.redistribute_member_account_balance).
    Если собственник был ЕДИНСТВЕННЫМ: ничего, кроме самой записи
    GarageOwnership, не трогаем — лицевые счета остаются на нём (активными)
    со всем, что на них есть — например, пока решается вопрос с
    наследством. Как только у гаража появится новый собственник, add_owner
    сам заархивирует эти счета и перенесёт остаток на новые — см.
    _archive_owner_accounts_and_reuse. До этого момента — гараж без
    собственника, это нормальное переходное состояние, автоматика ничего
    не решает за председателя.
    """
    database.db_session.add(GarageOwnershipEvent(
        garage_id=garage.id, person_id=ownership.person_id, event_type=GarageOwnershipEventType.REMOVED,
        share=None, comment=reason, created_by_user_id=user_id,
    ))
    audit.record(
        "garage_owner.remove",
        f"{ownership.person.full_name} выбыл(а) из собственников гаража №{garage.number}"
        + (f" (причина: {reason})" if reason else ""),
        entity_type="person", entity_id=ownership.person_id,
    )

    remaining = [o for o in garage.ownerships if o.id != ownership.id]
    if remaining:
        total_remaining_share = sum((o.share for o in remaining), Decimal("0"))
        if total_remaining_share > 0:
            new_shares = split_amount_by_shares(
                Decimal("1"), {o.person_id: (o.share / total_remaining_share) for o in remaining},
                precision=Decimal("0.00001"),
            )
            for o in remaining:
                new_share = new_shares[o.person_id]
                if new_share != o.share:
                    database.db_session.add(GarageOwnershipEvent(
                        garage_id=garage.id, person_id=o.person_id, event_type=GarageOwnershipEventType.SHARE_CHANGED,
                        share=new_share, created_by_user_id=user_id,
                        comment=_("Автоматический пересчёт доли при выбытии {name}").format(name=ownership.person.full_name),
                    ))
                    o.share = new_share

            departing_accounts = (
                database.db_session.query(MemberAccount)
                .filter_by(garage_id=garage.id, person_id=ownership.person_id)
                .all()
            )
            for acc in departing_accounts:
                redistribute_member_account_balance(acc, new_shares)
    # else: единственный собственник — лицевые счета не трогаем вовсе.

    database.db_session.delete(ownership)


@bp.route("/<int:garage_id>/owners/<int:ownership_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def remove_owner(garage_id, ownership_id):
    ownership = database.db_session.get(GarageOwnership, ownership_id)
    if ownership and ownership.garage_id == garage_id:
        comment = (request.form.get("comment") or "").strip() or None
        _remove_owner_and_redistribute(ownership.garage, ownership, comment, g.user.id)
        database.db_session.commit()
        flash(_("Собственник удалён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/ownership-history")
@roles_required(RoleEnum.BOARD)
def ownership_history(garage_id):
    """Полная история изменений собственников гаража — append-only журнал
    (см. models.GarageOwnershipEvent), отдельная страница, а не вкладка на
    самой карточке гаража, чтобы не перегружать и без того длинную
    страницу; печатная (кнопка «Печать», как на pd4/persons.statement)."""
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        flash(_("Гараж не найден."), "danger")
        return redirect(url_for("garages.list_garages"))
    events = (
        database.db_session.query(GarageOwnershipEvent)
        .filter_by(garage_id=garage.id)
        .options(joinedload(GarageOwnershipEvent.person), joinedload(GarageOwnershipEvent.created_by))
        .order_by(GarageOwnershipEvent.created_at.desc(), GarageOwnershipEvent.id.desc())
        .all()
    )
    return render_template("garages/ownership_history.html", garage=garage, events=events, today=dt.date.today())


@bp.route("/<int:garage_id>/contacts/add", methods=["POST"])
@login_required
def add_contact(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not is_owner_or_board(garage):
        abort(403)
    person_id = int(request.form["person_id"])
    relation = request.form.get("relation") or None
    database.db_session.add(GarageContact(garage_id=garage_id, person_id=person_id, relation=relation))
    database.db_session.commit()
    flash(_("Контактное лицо добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/contacts/<int:contact_id>/remove", methods=["POST"])
@login_required
def remove_contact(garage_id, contact_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not is_owner_or_board(garage):
        abort(403)
    contact = database.db_session.get(GarageContact, contact_id)
    if contact and contact.garage_id == garage_id:
        database.db_session.delete(contact)
        database.db_session.commit()
        flash(_("Контактное лицо удалено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/comment", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_comment(garage_id):
    """Комментарий к гаражу видит и меняет только правление — это внутренние заметки, не для собственников."""
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    garage.comment = request.form.get("comment") or None
    database.db_session.commit()
    flash(_("Комментарий обновлён."), "success")
    return redirect(request.referrer or url_for("garages.detail", garage_id=garage_id))


# ---------------------------------------------------------------------------
# Фото гаража
# ---------------------------------------------------------------------------

ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@bp.route("/<int:garage_id>/photos/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_photo(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash(_("Выберите файл фотографии."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_PHOTO_EXT:
        flash(_("Поддерживаются только изображения (jpg, png, webp, gif)."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    stored_name = f"{uuid.uuid4().hex}{ext}"
    upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))

    photo = GaragePhoto(garage_id=garage.id, file_path=stored_name, caption=request.form.get("caption") or None)
    database.db_session.add(photo)
    database.db_session.commit()
    flash(_("Фото добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/photos/<int:photo_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_photo(garage_id, photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None or photo.garage_id != garage_id:
        abort(404)

    photo.caption = request.form.get("caption") or None

    upload = request.files.get("file")
    if upload and upload.filename:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ALLOWED_PHOTO_EXT:
            flash(_("Поддерживаются только изображения (jpg, png, webp, gif)."), "danger")
            return redirect(url_for("garages.detail", garage_id=garage_id))
        old_path = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
        if os.path.exists(old_path):
            os.remove(old_path)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        upload.save(os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name))
        photo.file_path = stored_name

    database.db_session.commit()
    flash(_("Фото обновлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/<int:garage_id>/photos/<int:photo_id>/remove", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def remove_photo(garage_id, photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None or photo.garage_id != garage_id:
        abort(404)

    file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
    if os.path.exists(file_path):
        os.remove(file_path)
    database.db_session.delete(photo)
    database.db_session.commit()
    flash(_("Фото удалено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id))


@bp.route("/photos/<int:photo_id>/file")
@login_required
def photo_file(photo_id):
    photo = database.db_session.get(GaragePhoto, photo_id)
    if photo is None:
        abort(404)
    if not is_owner_or_board(photo.garage):
        abort(403)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], photo.file_path)


# ---------------------------------------------------------------------------
# Электричество: счётчики и показания
# ---------------------------------------------------------------------------

@bp.route("/<int:garage_id>/electricity/meter/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_electricity_meter(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    f = request.form
    installed = f.get("installed_date")
    sealed = f.get("sealed_date")
    initial_reading = f.get("initial_reading")

    meter = ElectricityMeter(
        garage_id=garage.id,
        meter_number=f["meter_number"],
        installed_date=dt.date.fromisoformat(installed) if installed else None,
        sealed_date=dt.date.fromisoformat(sealed) if sealed else None,
        initial_reading=parse_decimal(initial_reading) if initial_reading else None,
        meter_seal_number=f.get("meter_seal_number") or None,
        breaker_seal_number=f.get("breaker_seal_number") or None,
        comment=f.get("comment") or None,
    )
    database.db_session.add(meter)
    database.db_session.commit()
    flash(_("Счётчик добавлен."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/electricity/meter/<int:meter_id>/edit", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def edit_electricity_meter(garage_id, meter_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    meter = database.db_session.get(ElectricityMeter, meter_id)
    if meter is None or meter.garage_id != garage_id:
        abort(404)

    f = request.form
    installed = f.get("installed_date")
    sealed = f.get("sealed_date")
    initial_reading = f.get("initial_reading")

    meter.meter_number = f["meter_number"]
    meter.installed_date = dt.date.fromisoformat(installed) if installed else None
    meter.sealed_date = dt.date.fromisoformat(sealed) if sealed else None
    meter.initial_reading = parse_decimal(initial_reading) if initial_reading else None
    meter.meter_seal_number = f.get("meter_seal_number") or None
    meter.breaker_seal_number = f.get("breaker_seal_number") or None
    meter.comment = f.get("comment") or None

    database.db_session.commit()
    flash(_("Счётчик обновлён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/electricity/meter/<int:meter_id>/delete", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def delete_electricity_meter(garage_id, meter_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    meter = database.db_session.get(ElectricityMeter, meter_id)
    if meter is None or meter.garage_id != garage_id:
        abort(404)

    if meter.readings:
        flash(_("Нельзя удалить счётчик, по которому уже есть показания."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    audit.record("electricity_meter.delete", f"Удалён счётчик гаража №{garage.number}: {meter.meter_number}", entity_type="garage", entity_id=garage.id)
    database.db_session.delete(meter)
    database.db_session.commit()
    flash(_("Счётчик удалён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/electricity/reading/add", methods=["POST"])
@login_required
def add_electricity_reading(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not is_owner_or_board(garage):
        abort(403)

    current = _current_meter(garage)
    if current is None:
        flash(_("Сначала добавьте счётчик."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id))

    f = request.form
    reading_value = parse_decimal(f["reading"])
    reading_date = dt.date.today()

    # предыдущее показание этого счётчика (по дате) — или начальные показания счётчика, если это первая запись
    previous = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    baseline = previous.reading if previous else current.initial_reading
    if baseline is not None and reading_value <= baseline:
        flash(_(
            "Показания не могут быть меньше предыдущих ({baseline}). Если счётчик был заменён, сначала внесите новый прибор учёта.",
            baseline=str(baseline.quantize(Decimal("0.01"))),
        ), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    amount = None
    tariff = current_tariff(reading_date)
    tariff_rate = tariff.rate if tariff is not None else None
    if baseline is not None:
        delta = reading_value - baseline
        if tariff is not None and delta > 0:
            amount = (delta * tariff.rate).quantize(Decimal("0.01"))

    reading = ElectricityReading(
        meter_id=current.id,
        reading=reading_value,
        reading_date=reading_date,
        amount=amount,
        tariff=tariff_rate,
        comment=f.get("comment") or None,
    )
    database.db_session.add(reading)
    database.db_session.flush()  # получить reading.id для связи с начислением

    if amount is not None:
        electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
        if electricity_fee_type is not None:
            database.db_session.add(Charge(
                garage_id=garage.id,
                fee_type_id=electricity_fee_type.id,
                year=reading_date.year,
                amount=amount,
                reading_id=reading.id,
                comment=_("Начислено по показаниям от {date}", date=reading_date.isoformat()),
            ))
            database.db_session.flush()
            reallocate_garage_charges(garage)

    audit.record(
        "electricity_reading.add",
        f"Внесены показания счётчика гаража №{garage.number}: {reading_value}"
        + (f", начислено {audit.format_amount(amount)}" if amount is not None else ""),
        entity_type="garage", entity_id=garage.id,
    )
    database.db_session.commit()

    if amount is None:
        flash(_("Показания внесены. Сумма не рассчитана — задайте тариф на странице «Электроэнергия»."), "warning")
    else:
        flash(_("Показания внесены, начислено {amount} ₽.", amount=amount), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/electricity/readings/last/edit", methods=["POST"])
@login_required
def edit_last_reading(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not is_chairman():
        abort(403)

    current = _current_meter(garage)
    if current is None:
        abort(404)

    last_reading = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    if last_reading is None:
        abort(404)

    f = request.form
    try:
        new_value = parse_decimal(f["reading"])
    except InvalidOperation:
        flash(_("Некорректное значение показаний."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    previous = (
        database.db_session.query(ElectricityReading)
        .filter_by(meter_id=current.id)
        .filter(ElectricityReading.id != last_reading.id)
        .order_by(ElectricityReading.reading_date.desc(), ElectricityReading.id.desc())
        .first()
    )
    baseline = previous.reading if previous else current.initial_reading

    if baseline is not None and new_value < baseline:
        flash(_(
            "Показания не могут быть меньше предыдущих ({baseline}).",
            baseline=str(baseline.quantize(Decimal("0.01"))),
        ), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))

    tariff_rate = last_reading.tariff
    if tariff_rate is None:
        tariff = current_tariff(last_reading.reading_date)
        tariff_rate = tariff.rate if tariff is not None else None

    amount = None
    if baseline is not None and tariff_rate is not None:
        delta = new_value - baseline
        if delta > 0:
            amount = (delta * tariff_rate).quantize(Decimal("0.01"))

    last_reading.reading = new_value
    last_reading.tariff = tariff_rate
    if f.get("comment") is not None:
        last_reading.comment = f.get("comment") or None

    charge = last_reading.charge
    if amount is not None:
        if charge is not None:
            charge.amount = amount
        else:
            electricity_fee_type = database.db_session.query(FeeType).filter_by(code="electricity").first()
            if electricity_fee_type is not None:
                database.db_session.add(Charge(
                    garage_id=garage.id,
                    fee_type_id=electricity_fee_type.id,
                    year=last_reading.reading_date.year,
                    amount=amount,
                    reading_id=last_reading.id,
                    comment=_("Начислено по показаниям от {date}", date=last_reading.reading_date.isoformat()),
                ))
    elif charge is not None:
        garage.charges.remove(charge)

    audit.record(
        "electricity_reading.edit_last",
        f"Председатель исправил(а) последнее показание счётчика гаража №{garage.number} на {new_value}"
        + (f", начисление обновлено на {audit.format_amount(amount)}" if amount is not None
           else ", связанное начисление удалено" if charge is not None else ""),
        entity_type="garage", entity_id=garage.id,
    )
    database.db_session.flush()
    reallocate_garage_charges(garage)
    database.db_session.commit()
    flash(_("Последнее показание исправлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


# ---------------------------------------------------------------------------
# Номер счёта
# ---------------------------------------------------------------------------

@bp.route("/<int:garage_id>/account/number", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def update_account_number(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    account = garage.account
    if account is None:
        abort(404)
    account.account_number = request.form["account_number"].strip()
    try:
        database.db_session.commit()
    except Exception:
        database.db_session.rollback()
        flash(_("Такой номер счёта уже используется."), "danger")
        return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))
    flash(_("Номер счёта обновлён."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


# ---------------------------------------------------------------------------
# Гаражные начисления и платежи
# ---------------------------------------------------------------------------

@bp.route("/<int:garage_id>/charges/add", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def add_charge(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)

    f = request.form
    charge = Charge(
        garage_id=garage.id,
        fee_type_id=int(f["fee_type_id"]),
        year=int(f["year"]),
        amount=parse_decimal(f["amount"]),
    )
    database.db_session.add(charge)
    database.db_session.flush()
    reallocate_garage_charges(garage)
    audit.record(
        "charge.create", entity_type="garage", entity_id=garage.id,
        summary=f"Начисление {audit.format_amount(charge.amount)} на гараж №{garage.number}, {f['year']} год",
    )
    database.db_session.commit()
    flash(_("Начисление добавлено."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))


@bp.route("/<int:garage_id>/payments/add", methods=["POST"])
@login_required
def add_payment(garage_id):
    garage = database.db_session.get(Garage, garage_id)
    if garage is None:
        abort(404)
    if not is_privileged():
        abort(403)

    f = request.form
    payment = Payment(
        garage_id=garage.id,
        date=dt.date.fromisoformat(f["date"]),
        amount=parse_decimal(f["amount"]),
        payer_person_id=int(f["payer_person_id"]) if f.get("payer_person_id") else None,
        comment=f.get("comment") or None,
    )
    database.db_session.add(payment)
    database.db_session.flush()
    reallocate_garage_charges(garage)
    audit.record(
        "payment.create", entity_type="garage", entity_id=garage.id,
        summary=f"Платёж {audit.format_amount(payment.amount)} на гараж №{garage.number} "
                f"от {audit.format_date(payment.date)}",
    )
    database.db_session.commit()
    flash(_("Платёж зарегистрирован."), "success")
    return redirect(url_for("garages.detail", garage_id=garage_id, tab="account"))
