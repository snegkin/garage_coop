import json
import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from . import database
from . import audit
from .i18n import translate as _
from .auth import login_required, roles_required, is_safe_next_url
from .permissions import is_board, is_chairman, sync_user_role
from .models import (
    Person, Phone, User, RoleEnum, MemberAccount, PersonDataRevision, PersonDataRevisionStatus,
    GarageOwnership, PersonalAccount, Cooperative,
)
from sqlalchemy.orm import joinedload
from .accounting import balance

bp = Blueprint("persons", __name__, url_prefix="/persons")


@bp.route("/")
@roles_required(RoleEnum.BOARD)
def list_persons():
    q = request.args.get("q", "").strip()
    show_pending_only = request.args.get("pending") == "1"
    show_archived = request.args.get("archived") == "1"

    # Архивные (умер/продал гараж и т.п. — см. archive_person) скрыты из
    # общего реестра по умолчанию, но не удалены — доступны по прямой
    # ссылке на карточку/выписку и отдельной вкладкой здесь же.
    persons = (
        database.db_session.query(Person)
        .filter(Person.is_archived.is_(show_archived))
        .order_by(Person.full_name)
        .all()
    )

    # Находим всех, у кого есть pending-ревизии
    all_revisions = (
        database.db_session.query(PersonDataRevision)
        .filter_by(status=PersonDataRevisionStatus.PENDING)
        .order_by(PersonDataRevision.submitted_at.desc())
        .all()
    )
    pending_by_person: dict[int, PersonDataRevision] = {}
    for rev in all_revisions:
        if rev.person_id not in pending_by_person:
            pending_by_person[rev.person_id] = rev

    accounts_by_person = {}
    for account in database.db_session.query(MemberAccount).all():
        accounts_by_person.setdefault(account.person_id, []).append(account)
    balances = {
        person.id: (
            sum((balance(a) for a in accounts_by_person[person.id]), Decimal("0"))
            if person.id in accounts_by_person else None
        )
        for person in persons
    }

    # Если фильтр — только pending (не применяется вместе с «архивные» —
    # архивный человек pending-ревизий не подаёт)
    if show_pending_only:
        persons = [p for p in persons if p.id in pending_by_person]

    archived_count = database.db_session.query(Person).filter(Person.is_archived.is_(True)).count()
    active_count = database.db_session.query(Person).filter(Person.is_archived.is_(False)).count()

    return render_template(
        "persons/list.html",
        persons=persons,
        balances=balances,
        pending_by_person=pending_by_person,
        show_pending_only=show_pending_only,
        show_archived=show_archived,
        archived_count=archived_count,
        active_count=active_count,
        q=q,
    )


def _save_from_form(person, f):
    person.full_name = f["full_name"]
    person.registration_address = f.get("registration_address") or None
    person.residence_address = f.get("residence_address") or None
    person.email = f.get("email") or None
    person.telegram = f.get("telegram") or None
    person.passport_series = f.get("passport_series") or None
    person.passport_number = f.get("passport_number") or None
    issue_date = f.get("passport_issue_date")
    person.passport_issue_date = dt.date.fromisoformat(issue_date) if issue_date else None
    membership_start = f.get("membership_start_date")
    person.membership_start_date = dt.date.fromisoformat(membership_start) if membership_start else None
    membership_end = f.get("membership_end_date")
    person.membership_end_date = dt.date.fromisoformat(membership_end) if membership_end else None
    person.comment = f.get("comment") or None


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def create():
    if request.method == "POST":
        # Получаем имя из формы (предполагается, что поле называется full_name)
        form_name = request.form.get("full_name", "").strip()

        # 1. Проверяем, есть ли уже такой человек
        existing_person = database.db_session.query(Person).filter(
            Person.full_name.ilike(form_name)
        ).first()

        if existing_person:
            flash(f"Человек с именем «{form_name}» уже существует в базе.", "danger")
            # Возвращаем пользователя обратно на форму
            return render_template("persons/form.html", person=None)

        person = Person(full_name="")
        _save_from_form(person, request.form)
        database.db_session.add(person)

        try:
            database.db_session.flush()

            phones = [p.strip() for p in request.form.get("phones", "").split(",") if p.strip()]
            for number in phones:
                database.db_session.add(Phone(person_id=person.id, number=number))

            database.db_session.commit()
            flash(_("Человек «{name}» добавлен.", name=person.full_name), "success")

        except IntegrityError:
            # 2. Перехватываем ошибку, если кто-то успел создать запись в эту же миллисекунду
            database.db_session.rollback()
            flash("Произошла ошибка при сохранении (возможно, такой человек уже существует).", "danger")
            return render_template("persons/form.html", person=None)

        next_url = request.form.get("next")
        if is_safe_next_url(next_url):
            sep = "&" if "?" in next_url else "?"
            return redirect(f"{next_url}{sep}new_person_id={person.id}")
        return redirect(url_for("persons.detail", person_id=person.id))

    return render_template("persons/form.html", person=None)


# Поля, которые член кооператива может предложить изменить через ЛК (см.
# cabinet.profile) — те же ключи, что и в fields_snapshot ревизии. Порядок
# и подписи — как в форме редактирования персональных данных, чтобы
# сравнение в detail.html выглядело как та же форма, но с подсветкой того,
# что изменилось.
_REVISION_FIELDS = [
    ("email", "Email"),
    ("telegram", "Telegram"),
    ("registration_address", "Адрес регистрации"),
    ("residence_address", "Адрес проживания"),
    ("phones", "Телефоны"),
    ("passport_series", "Серия паспорта"),
    ("passport_number", "Номер паспорта"),
    ("passport_issue_date", "Дата выдачи"),
]


def _revision_diff_rows(revision, person):
    """Построчное сравнение snapshot-а ревизии с данными person ДО неё —
    чтобы председатель видел не сырой JSON, а понятную таблицу «было/стало»
    с подсветкой изменившихся полей. «Было» берём из revision.previous_snapshot
    (снимок карточки на момент отправки, см. cabinet.profile) — НЕ из текущих
    данных person: после одобрения карточка уже содержит те же значения, что
    и fields_snapshot, поэтому сравнение с live-данными для уже одобренной
    ревизии показало бы «было == стало». Фолбэк на текущие данные person —
    только для ревизий, созданных до появления previous_snapshot."""
    try:
        snap = json.loads(revision.fields_snapshot)
    except Exception:
        snap = {}

    if revision.previous_snapshot:
        try:
            current = json.loads(revision.previous_snapshot)
        except Exception:
            current = {}
    else:
        current = {
            "email": person.email,
            "telegram": person.telegram,
            "registration_address": person.registration_address,
            "residence_address": person.residence_address,
            "phones": sorted(p.number for p in person.phones),
            "passport_series": person.passport_series,
            "passport_number": person.passport_number,
            "passport_issue_date": person.passport_issue_date.isoformat() if person.passport_issue_date else None,
        }

    def fmt(key, value):
        if key == "phones":
            return ", ".join(value) if value else "—"
        if key == "passport_issue_date" and value:
            return dt.date.fromisoformat(value).strftime("%d.%m.%Y")
        return value or "—"

    rows = []
    for key, label in _REVISION_FIELDS:
        old_value = current.get(key)
        new_value = snap.get(key)
        rows.append({
            "label": label,
            "old": fmt(key, old_value),
            "new": fmt(key, new_value),
            "changed": old_value != new_value,
        })
    return rows


@bp.route("/<int:person_id>")
@login_required
def detail(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        flash(_("Человек не найден."), "danger")
        return redirect(url_for("persons.list_persons") if is_board() else url_for("cabinet.profile"))
    if not is_board() and g.user.person_id != person_id:
        abort(403)
    account = database.db_session.query(User).filter_by(person_id=person.id).first()
    # Пени без начислений (баланс — 0) НЕ отфильтровываются здесь — этим
    # занимается чекбокс «Актуальные» на клиенте (data-zero-penalty, см.
    # persons/detail.html), иначе для таких счетов чекбокс не работал бы:
    # строка не долетала бы до шаблона ни в каком состоянии галки.
    member_accounts = (
        database.db_session.query(MemberAccount)
        .filter_by(person_id=person.id)
        .options(joinedload(MemberAccount.charges))
        .all()
    )
    revision_rows = {}
    if is_chairman():
        revision_rows = {rev.id: _revision_diff_rows(rev, person) for rev in person.revisions}
    return render_template(
        "persons/detail.html", person=person, account=account, member_accounts=member_accounts,
        revision_rows=revision_rows, today=dt.date.today(),
    )


def _statement_row(account_number: str, url: str, label: str, charges, payments) -> dict:
    """Одна сводная строка выписки — один лицевой счёт, весь его баланс за
    всё время (сумма начислений/сумма платежей), без построчной разбивки
    по годам/платежам — за деталями (кто когда платил, комментарии к
    отдельным начислениям) правление и сам владелец счёта переходят по
    ссылке на карточку счёта (url)."""
    charged = sum((c.amount for c in charges), Decimal("0"))
    paid = sum((p.amount for p in payments), Decimal("0"))
    years = [c.year for c in charges]
    return {
        "account_number": account_number, "url": url, "label": label,
        "charged": charged, "paid": paid, "balance": paid - charged,
        "year_from": min(years) if years else None, "year_to": max(years) if years else None,
    }


@bp.route("/<int:person_id>/statement")
@login_required
def statement(person_id):
    """
    Сводная печатная выписка по ВСЕМ лицевым счетам человека сразу —
    взносы/налог (MemberAccount) и, если он собственник гаража(ей),
    электричество (PersonalAccount, счёт общий на гараж, не персональный,
    но раз человек им пользуется — включаем и его тоже). Одна строка на
    счёт (см. _statement_row) — не постатейно по каждому начислению/
    платежу, как было раньше: для печатной выписки, которую видит и
    рядовой член, это оказалось избыточно подробно (годы, статусы оплаты
    каждого начисления по отдельности), а полная история всё равно есть
    на карточке конкретного счёта, куда ведёт ссылка по номеру.

    Счета вида взноса «пеня» — отдельным блоком под основной таблицей
    (penalty_rows), не строкой в общем списке: их баланс расчётно другой
    природы (не долг за товар/услугу, а санкция за просрочку), поэтому
    итог считается явно в два слагаемых — «Баланс» (без пени) и «Пеня» —
    и складывается в penalty_rows/grand_total ниже.
    """
    person = database.db_session.get(Person, person_id)
    if person is None:
        flash(_("Человек не найден."), "danger")
        return redirect(url_for("persons.list_persons") if is_board() else url_for("cabinet.profile"))
    if not is_board() and g.user.person_id != person_id:
        abort(403)

    member_accounts = (
        database.db_session.query(MemberAccount)
        .filter_by(person_id=person.id)
        .options(joinedload(MemberAccount.charges), joinedload(MemberAccount.payments))
        .all()
    )
    # Печатная выписка — здесь нет чекбокса «Актуальные», который мог бы
    # раскрыть счёт обратно, поэтому пени без единого начисления (нечего
    # показывать) отфильтровываем насовсем, в отличие от detail() выше.
    member_accounts = [ma for ma in member_accounts if not ma.fee_type.is_penalty or ma.charges]
    member_accounts.sort(key=lambda ma: (ma.garage.number, ma.fee_type.name))

    owned_garage_ids = [
        o.garage_id for o in
        database.db_session.query(GarageOwnership).filter_by(person_id=person.id).order_by(GarageOwnership.id).all()
    ]
    personal_accounts = []
    if owned_garage_ids:
        personal_accounts = (
            database.db_session.query(PersonalAccount)
            .filter(PersonalAccount.garage_id.in_(owned_garage_ids))
            .options(joinedload(PersonalAccount.garage))
            .all()
        )
        personal_accounts.sort(key=lambda pa: pa.garage.number)

    rows, penalty_rows = [], []
    for ma in member_accounts:
        row = _statement_row(
            ma.account_number, url_for("finance.member_account_detail", account_id=ma.id),
            f"{ma.fee_type.name}, {_('гараж')} №{ma.garage.number}", ma.charges, ma.payments,
        )
        (penalty_rows if ma.fee_type.is_penalty else rows).append(row)
    for pa in personal_accounts:
        rows.append(_statement_row(
            pa.account_number, url_for("garages.detail", garage_id=pa.garage_id),
            f"{_('Электричество')}, {_('гараж')} №{pa.garage.number}", pa.garage.charges, pa.garage.payments,
        ))

    all_rows = rows + penalty_rows
    years = [r["year_from"] for r in all_rows if r["year_from"]] + [r["year_to"] for r in all_rows if r["year_to"]]
    balance_excl_penalty = sum((r["balance"] for r in rows), Decimal("0"))
    penalty_total = sum((r["balance"] for r in penalty_rows), Decimal("0"))

    return render_template(
        "persons/statement.html", person=person, rows=rows, penalty_rows=penalty_rows,
        balance_excl_penalty=balance_excl_penalty, penalty_total=penalty_total,
        grand_total=balance_excl_penalty + penalty_total,
        year_from=min(years) if years else None, year_to=max(years) if years else None,
        today=dt.date.today(),
        # Для официальной шапки/подписей на печатной форме (см.
        # persons/statement.html: .print-letterhead/.print-signatures) —
        # никак не влияют на суммы, только на оформление печатного листа.
        coop=database.db_session.query(Cooperative).first(),
        chairman=database.db_session.query(Person).filter(Person.is_chairman.is_(True)).first(),
        accountant=database.db_session.query(Person).filter(Person.is_accountant.is_(True)).first(),
    )


@bp.route("/<int:person_id>/archive", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def archive_person(person_id):
    """
    Отправляет человека в архив — скрывается из общего реестра (см.
    list_persons), но карточка/выписка остаются доступны по прямой ссылке
    («мало ли какие вопросы»). Синхронизировано с выбытием из
    собственников: для каждого гаража, где человек СОвладелец, он
    автоматически убирается из собственников (см.
    garages._remove_owner_and_redistribute) той же причиной — доли
    оставшихся пересчитываются, остаток его лицевых счетов по этому
    гаражу распределяется между ними пропорционально новым долям.
    Если он был ЕДИНСТВЕННЫМ собственником — гараж и его лицевые счета не
    трогаются вовсе (см. docstring _remove_owner_and_redistribute) —
    остаются как есть до появления нового собственника.
    """
    from .garages import _remove_owner_and_redistribute  # локальный импорт: избегаем цикла persons<->garages

    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)

    next_url = request.form.get("next")
    fallback_url = url_for("persons.detail", person_id=person.id)
    redirect_url = next_url if is_safe_next_url(next_url) else fallback_url

    if person.is_archived:
        flash(_("Этот человек уже в архиве."), "warning")
        return redirect(redirect_url)

    reason = (request.form.get("reason") or "").strip() or None

    ownerships = database.db_session.query(GarageOwnership).filter_by(person_id=person.id).all()
    for ownership in ownerships:
        _remove_owner_and_redistribute(ownership.garage, ownership, reason, g.user.id)

    person.is_archived = True
    person.archived_at = dt.datetime.utcnow()
    person.archived_reason = reason
    person.archived_by_user_id = g.user.id
    audit.record(
        "person.archive", entity_type="person", entity_id=person.id,
        summary=f"Человек «{person.full_name}» отправлен в архив" + (f": {reason}" if reason else ""),
    )
    database.db_session.commit()
    flash(_("Человек отправлен в архив."), "success")
    return redirect(redirect_url)


@bp.route("/<int:person_id>/unarchive", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def unarchive_person(person_id):
    """
    Возврат из архива — только снимает пометку, НЕ восстанавливает
    собственников гаражей автоматически (решить, с какой долей и на каких
    условиях — дело председателя, автоматика тут гадать не должна;
    добавить обратно можно обычной формой «Добавить собственника» на
    странице гаража).
    """
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    person.is_archived = False
    person.archived_at = None
    person.archived_reason = None
    person.archived_by_user_id = None
    audit.record(
        "person.unarchive", entity_type="person", entity_id=person.id,
        summary=f"Человек «{person.full_name}» возвращён из архива",
    )
    database.db_session.commit()
    flash(_("Человек возвращён из архива."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.BOARD)
def edit(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        flash(_("Человек не найден."), "danger")
        return redirect(url_for("persons.list_persons"))

    if request.method == "POST":
        _save_from_form(person, request.form)

        # телефоны просто пересобираем заново из строки через запятую
        for phone in list(person.phones):
            database.db_session.delete(phone)
        phones = [p.strip() for p in request.form.get("phones", "").split(",") if p.strip()]
        for number in phones:
            database.db_session.add(Phone(person_id=person.id, number=number))

        database.db_session.commit()
        flash(_("Изменения сохранены."), "success")
        return redirect(url_for("persons.detail", person_id=person.id))

    return render_template("persons/form.html", person=person)


@bp.route("/<int:person_id>/account/create", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def create_account(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)

    existing = database.db_session.query(User).filter_by(person_id=person.id).first()
    if existing:
        flash(_("У этого человека уже есть учётная запись."), "warning")
        return redirect(url_for("persons.detail", person_id=person.id))

    username = request.form["username"].strip()
    password = request.form["password"]

    if database.db_session.query(User).filter_by(username=username).first():
        flash(_("Такой логин уже занят."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

    initial_role = RoleEnum.CHAIRMAN if person.is_chairman else (
        RoleEnum.ACCOUNTANT if person.is_accountant else (
            RoleEnum.BOARD if person.is_board_member else RoleEnum.MEMBER
        )
    )
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=initial_role,
        person_id=person.id,
        is_active=True,
    )
    database.db_session.add(user)
    database.db_session.commit()
    flash(_("Учётная запись создана. Сообщите человеку логин и пароль."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/account/reset-password", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def reset_password(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        abort(404)

    user.password_hash = generate_password_hash(request.form["password"])
    audit.record(
        "account.password_reset", entity_type="user", entity_id=user.id,
        summary=f"Правление сбросило пароль пользователю «{user.username}» ({person.full_name})",
    )
    database.db_session.commit()
    flash(_("Пароль обновлён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/account/change-username", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def change_username(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        abort(404)

    new_username = request.form["username"].strip()
    if not new_username:
        flash(_("Логин не может быть пустым."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

    conflict = database.db_session.query(User).filter(
        User.username == new_username, User.id != user.id
    ).first()
    if conflict:
        flash(_("Такой логин уже занят."), "danger")
        return redirect(url_for("persons.detail", person_id=person.id))

    old_username = user.username
    user.username = new_username
    audit.record(
        "account.username_change", entity_type="user", entity_id=user.id,
        summary=f"Логин «{old_username}» ({person.full_name}) изменён на «{new_username}»",
    )
    database.db_session.commit()
    flash(_("Логин обновлён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/account/toggle-active", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def toggle_active(person_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        abort(404)

    user.is_active = not user.is_active
    audit.record(
        "account.toggle_active", entity_type="user", entity_id=user.id,
        summary=f"Доступ пользователю «{user.username}» ({person.full_name}) {'включён' if user.is_active else 'отключён'}",
    )
    database.db_session.commit()
    flash(_("Доступ включён.") if user.is_active else _("Доступ отключён."), "success")
    return redirect(url_for("persons.detail", person_id=person.id))


# ---------------------------------------------------------------------------
# Управление учётными записями: привязка/отвязка к человеку (только председатель)
# ---------------------------------------------------------------------------

@bp.route("/accounts")
@roles_required(RoleEnum.CHAIRMAN)
def accounts_list():
    users = database.db_session.query(User).order_by(User.username).all()
    unlinked_persons = (
        database.db_session.query(Person)
        .outerjoin(User, User.person_id == Person.id)
        .filter(User.id.is_(None))
        .order_by(Person.full_name)
        .all()
    )
    return render_template("persons/accounts.html", users=users, unlinked_persons=unlinked_persons)


@bp.route("/accounts/<int:user_id>/link", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def link_account(user_id):
    user = database.db_session.get(User, user_id)
    if user is None:
        abort(404)
    if user.person_id is not None:
        flash(_("Эта учётная запись уже привязана к человеку — сначала отвяжите."), "warning")
        return redirect(url_for("persons.accounts_list"))

    person_id = request.form.get("person_id", type=int)
    person = database.db_session.get(Person, person_id) if person_id else None
    if person is None:
        flash(_("Выберите человека для привязки."), "danger")
        return redirect(url_for("persons.accounts_list"))

    existing = database.db_session.query(User).filter_by(person_id=person.id).first()
    if existing:
        flash(_("У этого человека уже есть другая учётная запись."), "warning")
        return redirect(url_for("persons.accounts_list"))

    user.person_id = person.id
    database.db_session.flush()
    sync_user_role(person)  # роль подтягивается под флаги этого человека
    database.db_session.commit()
    flash(_("Учётная запись привязана."), "success")
    return redirect(url_for("persons.accounts_list"))


@bp.route("/accounts/<int:user_id>/unlink", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def unlink_account(user_id):
    user = database.db_session.get(User, user_id)
    if user is None:
        abort(404)
    user.person_id = None
    database.db_session.commit()
    flash(_("Учётная запись отвязана от человека."), "success")
    return redirect(url_for("persons.accounts_list"))


# ---------------------------------------------------------------------------
# Одобрение / отклонение изменений персональных данных (только председатель)
# ---------------------------------------------------------------------------

def _apply_revision(revision):
    """Применяет одобренные данные из ревизии к карточке Person."""
    person = database.db_session.get(Person, revision.person_id)
    if person is None:
        return
    try:
        snap = json.loads(revision.fields_snapshot)
    except Exception:
        return
    person.email = snap.get("email")
    person.telegram = snap.get("telegram")
    person.registration_address = snap.get("registration_address")
    person.residence_address = snap.get("residence_address")
    # телефоны
    for phone in list(person.phones):
        database.db_session.delete(phone)
    for number in snap.get("phones", []):
        database.db_session.add(Phone(person_id=person.id, number=number))
    # паспорт
    person.passport_series = snap.get("passport_series")
    person.passport_number = snap.get("passport_number")
    issue_date_str = snap.get("passport_issue_date")
    if issue_date_str:
        try:
            person.passport_issue_date = dt.date.fromisoformat(issue_date_str)
        except (ValueError, TypeError):
            pass
    else:
        person.passport_issue_date = None


@bp.route("/persons/<int:person_id>/revisions/approve/<int:revision_id>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def approve_revision(person_id, revision_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    revision = database.db_session.get(PersonDataRevision, revision_id)
    if revision is None or revision.person_id != person_id:
        abort(404)
    if revision.status != PersonDataRevisionStatus.PENDING:
        flash(_("Эта ревизия уже обработана."), "warning")
        return redirect(url_for("persons.list_persons"))

    _apply_revision(revision)
    revision.status = PersonDataRevisionStatus.APPROVED
    revision.reviewed_at = dt.datetime.utcnow()
    revision.reviewer_user_id = g.user.id
    database.db_session.commit()
    flash(_("Изменения для «{name}» одобрены и применены.", name=person.full_name), "success")
    return redirect(url_for("persons.list_persons"))


@bp.route("/persons/<int:person_id>/revisions/reject/<int:revision_id>", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def reject_revision(person_id, revision_id):
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    revision = database.db_session.get(PersonDataRevision, revision_id)
    if revision is None or revision.person_id != person_id:
        abort(404)
    if revision.status != PersonDataRevisionStatus.PENDING:
        flash(_("Эта ревизия уже обработана."), "warning")
        return redirect(url_for("persons.list_persons"))

    revision.status = PersonDataRevisionStatus.REJECTED
    revision.reviewed_at = dt.datetime.utcnow()
    revision.reviewer_user_id = g.user.id
    database.db_session.commit()
    flash(_("Изменения для «{name}» отклонены.", name=person.full_name), "info")
    return redirect(url_for("persons.list_persons"))
