import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from . import database
from . import audit
from .i18n import translate as _
from .auth import login_required, roles_required
from .permissions import sync_user_role
from .models import (
    BoardTerm, BoardMember, RevisionCommission, RevisionCommissionMember,
    Person, GeneralMeeting, RoleEnum, AuditLog,
)

bp = Blueprint("governance", __name__, url_prefix="/governance")


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------

def _current_term():
    """Текущий (не закрытый) созыв правления — последний по дате начала среди тех, у кого нет end_date."""
    return (
        database.db_session.query(BoardTerm)
        .filter(BoardTerm.end_date.is_(None))
        .order_by(BoardTerm.start_date.desc())
        .first()
    )


def current_board_member_ids() -> set[int]:
    """
    ID людей — членов текущего (не закрытого) созыва правления, включая
    председателя. Публичная обёртка над _current_term() для использования
    вне этого модуля — см. proposals.py: кто голосует за одобрение
    предложенных членами кооператива голосований.
    """
    term = _current_term()
    return {m.person_id for m in term.members} if term else set()


def _current_commission():
    return (
        database.db_session.query(RevisionCommission)
        .filter(RevisionCommission.end_date.is_(None))
        .order_by(RevisionCommission.start_date.desc())
        .first()
    )


@bp.route("/")
@login_required
def view():
    terms = database.db_session.query(BoardTerm).order_by(BoardTerm.start_date.desc()).all()
    commissions = database.db_session.query(RevisionCommission).order_by(RevisionCommission.start_date.desc()).all()
    meetings = database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).all()
    persons = database.db_session.query(Person).order_by(Person.full_name).all()
    return render_template(
        "governance/view.html",
        terms=terms,
        commissions=commissions,
        meetings=meetings,
        persons=persons,
        current_commission=_current_commission(),
        current_accountant=database.db_session.query(Person).filter_by(is_accountant=True).first(),
    )


@bp.route("/contacts")
@login_required
def contacts():
    """Контакты действующего правления — вынесены с общей страницы «Правление»
    (созывы/ревизионная комиссия/бухгалтер) в отдельную, т.к. это открытые
    данные, к которым обращаются чаще и без остального контекста."""
    return render_template("governance/contacts.html", current_term=_current_term())


# ---------------------------------------------------------------------------
# Бухгалтер — назначается председателем, не общим собранием: это не
# выборная должность, поэтому вне созывов правления и без протокола.
# Бухгалтер не обязан быть членом правления (может быть на аутсорсе) —
# в списке для назначения все люди, не только состав текущего созыва.
# ---------------------------------------------------------------------------

@bp.route("/accountant/set", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def set_accountant():
    person_id = int(request.form["person_id"])
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)

    for p in database.db_session.query(Person).filter(Person.is_accountant.is_(True), Person.id != person.id).all():
        p.is_accountant = False
        sync_user_role(p)

    person.is_accountant = True
    sync_user_role(person)
    audit.record("accountant.set", f"Бухгалтером назначен: {person.full_name}", entity_type="person", entity_id=person.id)
    database.db_session.commit()
    flash(_("Бухгалтер назначен."), "success")
    return redirect(url_for("governance.view"))


@bp.route("/accountant/unset", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def unset_accountant():
    person_id = int(request.form["person_id"])
    person = database.db_session.get(Person, person_id)
    if person is None:
        abort(404)
    person.is_accountant = False
    sync_user_role(person)
    audit.record("accountant.unset", f"Бухгалтер снят с должности: {person.full_name}", entity_type="person", entity_id=person.id)
    database.db_session.commit()
    flash(_("Бухгалтер снят с должности."), "success")
    return redirect(url_for("governance.view"))


# ---------------------------------------------------------------------------
# Созывы правления
# ---------------------------------------------------------------------------

def _apply_board_term_flags(term: BoardTerm) -> int:
    """
    Пересчитывает флаги is_board_member/is_chairman у ВСЕХ людей по составу
    переданного созыва — сбрасывает всем, кто в списке не состоит,
    проставляет по списку. Синхронизирует роль привязанной учётной записи
    (User.role) через permissions.sync_user_role() для каждого затронутого
    человека. is_accountant НЕ трогает — бухгалтера назначает председатель
    отдельно (см. set_accountant/unset_accountant ниже), общее собрание его
    не избирает, и он не обязан быть частью состава правления.

    Не вызывается автоматически при добавлении/правке отдельного члена
    созыва — только явно, отдельной кнопкой «Применить состав» на странице
    созыва. Если пересчитывать при каждой правке состава, председатель,
    ещё не успевший добавить в новый созыв самого себя, рисковал бы на
    полпути потерять доступ к этой же странице (роль в текущей сессии
    берётся из БД на каждый запрос) — поэтому применение состава к правам
    доступа осознанно отделено от самого редактирования списка.
    Возвращает число затронутых записей (для сообщения пользователю).
    """
    member_ids = {m.person_id for m in term.members}
    chairman_ids = {m.person_id for m in term.members if m.is_chairman}

    currently_flagged = database.db_session.query(Person).filter(
        Person.is_board_member.is_(True) | Person.is_chairman.is_(True)
    ).all()
    touched = {p.id: p for p in currently_flagged}
    for pid in member_ids:
        touched.setdefault(pid, database.db_session.get(Person, pid))

    for person in touched.values():
        if person is None:
            continue
        person.is_board_member = person.id in member_ids
        person.is_chairman = person.id in chairman_ids
        sync_user_role(person)

    return len(touched)


@bp.route("/board-terms/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create_term():
    f = request.form
    if not f.get("elected_by_meeting_id"):
        flash(_("Укажите протокол общего собрания, которым избран новый созыв."), "danger")
        return redirect(url_for("governance.view"))

    start_date = dt.date.fromisoformat(f["start_date"])

    previous = _current_term()
    if previous is not None:
        previous.end_date = start_date

    term = BoardTerm(start_date=start_date, elected_by_meeting_id=int(f["elected_by_meeting_id"]))
    database.db_session.add(term)
    audit.record("board_term.create", f"Открыт новый созыв правления с {audit.format_date(start_date)}")
    database.db_session.commit()
    flash(_("Созыв правления добавлен. Теперь внесите его состав."), "success")
    return redirect(url_for("governance.term_detail", term_id=term.id))


@bp.route("/board-terms/<int:term_id>")
@login_required
def term_detail(term_id):
    term = database.db_session.get(BoardTerm, term_id)
    if term is None:
        abort(404)
    persons = database.db_session.query(Person).order_by(Person.full_name).all()
    return render_template(
        "governance/term_detail.html",
        term=term,
        is_current=(term.id == _current_term().id if _current_term() else False),
        persons=persons,
    )


@bp.route("/board-terms/<int:term_id>/close", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def close_term(term_id):
    term = database.db_session.get(BoardTerm, term_id)
    if term is None:
        abort(404)
    end_date = request.form.get("end_date")
    term.end_date = dt.date.fromisoformat(end_date) if end_date else dt.date.today()
    audit.record("board_term.close", f"Закрыт созыв правления от {audit.format_date(term.start_date)}, дата закрытия {audit.format_date(term.end_date)}")
    database.db_session.commit()
    flash(_("Созыв закрыт."), "success")
    return redirect(url_for("governance.term_detail", term_id=term.id))


@bp.route("/board-terms/<int:term_id>/apply", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def apply_term(term_id):
    term = database.db_session.get(BoardTerm, term_id)
    if term is None:
        abort(404)
    if not term.members:
        flash(_("В созыве пока нет ни одного члена — сначала внесите состав."), "warning")
        return redirect(url_for("governance.term_detail", term_id=term.id))

    count = _apply_board_term_flags(term)
    audit.record("board_term.apply", f"Состав созыва правления от {audit.format_date(term.start_date)} применён к правам доступа, затронуто записей: {count}")
    database.db_session.commit()
    flash(_("Состав применён к правам доступа: обновлено записей — {count}.", count=count), "success")
    return redirect(url_for("governance.term_detail", term_id=term.id))


@bp.route("/board-terms/<int:term_id>/members/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def add_board_member(term_id):
    term = database.db_session.get(BoardTerm, term_id)
    if term is None:
        abort(404)

    f = request.form
    person_id = int(f["person_id"])
    if any(m.person_id == person_id for m in term.members):
        flash(_("Этот человек уже внесён в состав созыва."), "warning")
        return redirect(url_for("governance.term_detail", term_id=term.id))

    is_chairman = bool(f.get("is_chairman"))
    if is_chairman:
        for m in term.members:
            m.is_chairman = False

    person = database.db_session.get(Person, person_id)
    database.db_session.add(BoardMember(
        term_id=term.id,
        person_id=person_id,
        is_chairman=is_chairman,
        role=f.get("role") or None,
    ))
    audit.record(
        "board_term.member_add",
        f"{person.full_name} добавлен(а) в состав созыва правления от {audit.format_date(term.start_date)}"
        + (" (председатель)" if is_chairman else ""),
        entity_type="person", entity_id=person_id,
    )
    database.db_session.commit()
    flash(_("Человек добавлен в состав созыва."), "success")
    return redirect(url_for("governance.term_detail", term_id=term.id))


@bp.route("/board-terms/<int:term_id>/members/<int:member_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit_board_member(term_id, member_id):
    member = database.db_session.get(BoardMember, member_id)
    if member is None or member.term_id != term_id:
        abort(404)

    f = request.form
    is_chairman = bool(f.get("is_chairman"))
    if is_chairman and not member.is_chairman:
        for m in member.term.members:
            if m.id != member.id:
                m.is_chairman = False
    member.is_chairman = is_chairman
    member.role = f.get("role") or None
    audit.record(
        "board_term.member_edit",
        f"Изменена запись состава созыва правления: {member.person.full_name}"
        + (" (председатель)" if is_chairman else ""),
        entity_type="person", entity_id=member.person_id,
    )
    database.db_session.commit()
    flash(_("Запись изменена."), "success")
    return redirect(url_for("governance.term_detail", term_id=term_id))


@bp.route("/board-terms/<int:term_id>/members/<int:member_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_board_member(term_id, member_id):
    member = database.db_session.get(BoardMember, member_id)
    if member is None or member.term_id != term_id:
        abort(404)
    audit.record(
        "board_term.member_delete",
        f"{member.person.full_name} убран(а) из состава созыва правления от {audit.format_date(member.term.start_date)}",
        entity_type="person", entity_id=member.person_id,
    )
    database.db_session.delete(member)
    database.db_session.commit()
    flash(_("Человек убран из состава созыва."), "success")
    return redirect(url_for("governance.term_detail", term_id=term_id))


# ---------------------------------------------------------------------------
# Ревизионная комиссия
# ---------------------------------------------------------------------------

@bp.route("/revision-commissions/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create_commission():
    f = request.form
    if not f.get("elected_by_meeting_id"):
        flash(_("Укажите протокол общего собрания, которым избрана комиссия."), "danger")
        return redirect(url_for("governance.view"))

    start_date = dt.date.fromisoformat(f["start_date"])

    previous = _current_commission()
    if previous is not None:
        previous.end_date = start_date

    commission = RevisionCommission(start_date=start_date, elected_by_meeting_id=int(f["elected_by_meeting_id"]))
    database.db_session.add(commission)
    audit.record("revision_commission.create", f"Создана ревизионная комиссия с {audit.format_date(start_date)}")
    database.db_session.commit()
    flash(_("Ревизионная комиссия добавлена. Теперь внесите её состав."), "success")
    return redirect(url_for("governance.commission_detail", commission_id=commission.id))


@bp.route("/revision-commissions/<int:commission_id>")
@login_required
def commission_detail(commission_id):
    commission = database.db_session.get(RevisionCommission, commission_id)
    if commission is None:
        abort(404)
    persons = database.db_session.query(Person).order_by(Person.full_name).all()
    current_board_ids = {m.person_id for m in _current_term().members} if _current_term() else set()
    return render_template(
        "governance/commission_detail.html",
        commission=commission,
        is_current=(commission.id == _current_commission().id if _current_commission() else False),
        persons=persons,
        current_board_ids=current_board_ids,
    )


@bp.route("/revision-commissions/<int:commission_id>/close", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def close_commission(commission_id):
    commission = database.db_session.get(RevisionCommission, commission_id)
    if commission is None:
        abort(404)
    end_date = request.form.get("end_date")
    commission.end_date = dt.date.fromisoformat(end_date) if end_date else dt.date.today()
    audit.record("revision_commission.close", f"Закрыта ревизионная комиссия от {audit.format_date(commission.start_date)}, дата закрытия {audit.format_date(commission.end_date)}")
    database.db_session.commit()
    flash(_("Состав ревизионной комиссии закрыт."), "success")
    return redirect(url_for("governance.commission_detail", commission_id=commission.id))


@bp.route("/revision-commissions/<int:commission_id>/members/new", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def add_commission_member(commission_id):
    commission = database.db_session.get(RevisionCommission, commission_id)
    if commission is None:
        abort(404)

    f = request.form
    person_id = int(f["person_id"])
    if any(m.person_id == person_id for m in commission.members):
        flash(_("Этот человек уже внесён в состав комиссии."), "warning")
        return redirect(url_for("governance.commission_detail", commission_id=commission.id))

    current_term = _current_term()
    if current_term and any(m.person_id == person_id for m in current_term.members):
        flash(_(
            "Обратите внимание: этот человек сейчас числится в действующем составе правления — "
            "по уставу ревизионная комиссия обычно должна быть независима от правления. "
            "Запись всё равно добавлена, проверьте по вашему уставу."
        ), "warning")

    is_chair = bool(f.get("is_chair"))
    if is_chair:
        for m in commission.members:
            m.is_chair = False

    person = database.db_session.get(Person, person_id)
    database.db_session.add(RevisionCommissionMember(
        commission_id=commission.id, person_id=person_id, is_chair=is_chair,
    ))
    audit.record(
        "revision_commission.member_add",
        f"{person.full_name} добавлен(а) в состав ревизионной комиссии от {audit.format_date(commission.start_date)}"
        + (" (председатель комиссии)" if is_chair else ""),
        entity_type="person", entity_id=person_id,
    )
    database.db_session.commit()
    flash(_("Человек добавлен в состав комиссии."), "success")
    return redirect(url_for("governance.commission_detail", commission_id=commission.id))


@bp.route("/revision-commissions/<int:commission_id>/members/<int:member_id>/edit", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit_commission_member(commission_id, member_id):
    member = database.db_session.get(RevisionCommissionMember, member_id)
    if member is None or member.commission_id != commission_id:
        abort(404)

    is_chair = bool(request.form.get("is_chair"))
    if is_chair and not member.is_chair:
        for m in member.commission.members:
            if m.id != member.id:
                m.is_chair = False
    member.is_chair = is_chair
    audit.record(
        "revision_commission.member_edit",
        f"Изменена запись состава ревизионной комиссии: {member.person.full_name}"
        + (" (председатель комиссии)" if is_chair else ""),
        entity_type="person", entity_id=member.person_id,
    )
    database.db_session.commit()
    flash(_("Запись изменена."), "success")
    return redirect(url_for("governance.commission_detail", commission_id=commission_id))


@bp.route("/revision-commissions/<int:commission_id>/members/<int:member_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_commission_member(commission_id, member_id):
    member = database.db_session.get(RevisionCommissionMember, member_id)
    if member is None or member.commission_id != commission_id:
        abort(404)
    audit.record(
        "revision_commission.member_delete",
        f"{member.person.full_name} убран(а) из состава ревизионной комиссии от {audit.format_date(member.commission.start_date)}",
        entity_type="person", entity_id=member.person_id,
    )
    database.db_session.delete(member)
    database.db_session.commit()
    flash(_("Человек убран из состава комиссии."), "success")
    return redirect(url_for("governance.commission_detail", commission_id=commission_id))


# ---------------------------------------------------------------------------
# Журнал аудита
# ---------------------------------------------------------------------------

@bp.route("/audit-log")
@roles_required(RoleEnum.BOARD)
def audit_log():
    """
    Журнал денежных/ролевых/учётных действий (см. app/audit.py) — доступен
    всему правлению (не только председателю), это инструмент подотчётности,
    а не управления. Клиентская пагинация и поиск — для реального объёма
    событий кооператива (десятки-сотни в месяц).
    """
    entries = database.db_session.query(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).all()
    return render_template(
        "governance/audit_log.html", entries=entries,
    )
