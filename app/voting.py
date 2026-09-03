"""
Голосование — три вида (VoteType): заочное, очно-заочное и полностью очное.
Заочное/очно-заочное — электронное голосование по повестке вопросов; вес
голоса человека = сумма его долей владения по всем его гаражам — см.
person_voting_weight(). Так «1 гараж — 1 голос» и «при нескольких
собственниках голос делится по долям» получаются автоматически из уже
существующего GarageOwnership.share, без отдельного кода на этот случай
(см. подробный комментарий у моделей Vote/VoteQuestion/VoteBallot в
models.py).

Организационная модель для заочного/очно-заочного:
- Председатель создаёт голосование (статус draft) и формирует повестку
  (один или несколько вопросов, у каждого — свой порог принятия).
- Открывает голосование (draft -> open) — с этого момента и до closes_at
  члены могут подавать/менять бюллетень.
- Переголосование, пока голосование открыто, разрешено — обновляет тот же
  бюллетень, не создаёт дубль (см. cast_ballots).
- Для очно-заочного голосования часть членов голосует очно на собрании
  (на бумаге) — их волеизъявление в систему не попадает само по себе;
  председатель может внести/поправить бюллетень любого члена вручную,
  пока голосование open (см. set_ballot_for_person) — это тот же
  cast_ballots(), что и при самостоятельной подаче бюллетеня, поэтому
  переголосование самим членом после ручной записи председателем тоже
  работает как обычно (последняя подача побеждает).
- Председатель закрывает голосование явно (open -> closed) — либо раньше
  closes_at (досрочно, если, например, все уже проголосовали), либо позже.
  Приём бюллетеней прекращается и при истечении closes_at, даже если
  председатель не успел нажать «закрыть» (см. is_accepting_ballots).
- Итоговый протокол (подписанный) прикрепляется как обычный Document —
  так же, как для очных собраний (см. meetings.py) — это отдельный
  юридический документ, генерировать автоматически не пытаемся.

Полностью очное голосование (VoteType.IN_PERSON) — решение принято на
собрании, электронная повестка/бюллетени не заводятся вовсе: председатель
сразу при создании прикрепляет протокол с результатами, и Vote создаётся
уже в статусе CLOSED (см. create()).
"""
import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, current_app

from . import database
from . import audit
from .i18n import translate as _, parse_decimal
from .auth import login_required, roles_required
from .permissions import is_board, is_chairman
from .models import (
    Vote, VoteQuestion, VoteBallot, VoteType, VoteStatus, VoteChoice, QUORUM_THRESHOLD,
    GarageOwnership, Person, GeneralMeeting, Document, DocumentType, RoleEnum,
)
from .uploads import save_upload

bp = Blueprint("voting", __name__, url_prefix="/voting")


# ---------------------------------------------------------------------------
# Вес голоса и подсчёт результатов
# ---------------------------------------------------------------------------

def person_voting_weight(person_id: int) -> Decimal:
    """Вес голоса человека — сумма его долей владения по всем гаражам (текущая, на сейчас)."""
    ownerships = database.db_session.query(GarageOwnership).filter_by(person_id=person_id).all()
    return sum((o.share for o in ownerships), Decimal("0"))


def total_cooperative_weight() -> Decimal:
    """
    Суммарный вес голосов всех членов кооператива — сумма долей всех
    гаражей. По инварианту (доли одного гаража в сумме = 1) численно равно
    количеству гаражей с хотя бы одним собственником, но считаем через
    сумму долей напрямую — устойчиво даже если у какого-то гаража сумма
    долей отличается от 1 из-за ошибки в данных.
    """
    ownerships = database.db_session.query(GarageOwnership).all()
    return sum((o.share for o in ownerships), Decimal("0"))


def is_accepting_ballots(vote: Vote) -> bool:
    """Можно ли сейчас подавать/менять бюллетень — открыто и в пределах окна приёма."""
    now = dt.datetime.now()
    return vote.status == VoteStatus.OPEN and vote.opens_at <= now <= vote.closes_at


def question_results(question: VoteQuestion) -> dict:
    """
    Итоги по одному вопросу: суммы весов по каждому варианту, вес принявших
    участие и прошло ли решение. Доля "за" считается ВСЕГДА от общего веса
    голосов кооператива (total_cooperative_weight), а не от числа
    проголосовавших — это то же значение, что и знаменатель кворума (см.
    quorum_met), так что оба показателя согласованы между собой. Даже если
    формально "за" набралось достаточно, решение не считается принятым,
    если по голосованию в целом нет кворума (>50% участия) — иначе
    голосование двух активных членов при полном игнорировании остальными
    могло бы формально "принять" решение, набрав нужную долю от общего
    числа голосов чисто по счастливой случайности состава кооператива.
    """
    totals = {VoteChoice.FOR: Decimal("0"), VoteChoice.AGAINST: Decimal("0"), VoteChoice.ABSTAIN: Decimal("0")}
    for ballot in question.ballots:
        totals[ballot.choice] += ballot.weight
    participating = totals[VoteChoice.FOR] + totals[VoteChoice.AGAINST] + totals[VoteChoice.ABSTAIN]
    total = total_cooperative_weight()
    has_quorum = quorum_met(question.vote)
    passed = bool(has_quorum) and total > 0 and (totals[VoteChoice.FOR] / total) >= question.majority_threshold
    return {
        "for": totals[VoteChoice.FOR], "against": totals[VoteChoice.AGAINST], "abstain": totals[VoteChoice.ABSTAIN],
        "participating": participating, "total": total, "passed": passed,
    }


def vote_participation_weight(vote: Vote) -> Decimal:
    """
    Суммарный вес участников, подавших бюллетень хотя бы по одному вопросу
    этого голосования — знаменатель для проверки кворума (см. quorum_met).
    """
    weight_by_person: dict[int, Decimal] = {}
    for question in vote.questions:
        for ballot in question.ballots:
            weight_by_person[ballot.person_id] = max(weight_by_person.get(ballot.person_id, Decimal("0")), ballot.weight)
    return sum(weight_by_person.values(), Decimal("0"))


def quorum_met(vote: Vote) -> bool:
    """
    Кворум — жёсткое правило (QUORUM_THRESHOLD = 50%, не настраивается за
    голосование): правомочно, только если приняло участие СТРОГО БОЛЬШЕ
    половины от общего веса голосов кооператива.
    """
    total = total_cooperative_weight()
    if total <= 0:
        return False
    return (vote_participation_weight(vote) / total) > QUORUM_THRESHOLD


def person_ballots_by_question(vote: Vote, person_id: int) -> dict[int, VoteBallot]:
    """{question_id: VoteBallot} уже поданных этим человеком бюллетеней по этому голосованию."""
    result = {}
    for question in vote.questions:
        for ballot in question.ballots:
            if ballot.person_id == person_id:
                result[question.id] = ballot
    return result


def eligible_voters() -> list[Person]:
    """
    Все люди с ненулевой долей владения хотя бы в одном гараже (т.е.
    person_voting_weight > 0) — потенциальные участники голосования,
    отсортированы по ФИО. Используется для ручной записи председателем
    очных голосов по очно-заочному голосованию (см. set_ballot_for_person).
    """
    ownerships = database.db_session.query(GarageOwnership).all()
    person_ids = {o.person_id for o in ownerships if o.share and o.share > 0}
    if not person_ids:
        return []
    return (
        database.db_session.query(Person)
        .filter(Person.id.in_(person_ids))
        .order_by(Person.full_name)
        .all()
    )


def cast_ballots(
    vote: Vote, person: Person, choices: dict[int, VoteChoice], comments: dict[int, str] | None = None,
) -> None:
    """
    Подаёт/обновляет бюллетень человека по всем переданным вопросам этого
    голосования за один раз (upsert по (question_id, person_id) — см.
    UniqueConstraint на VoteBallot). Вес пересчитывается заново на каждую
    подачу — не «замораживается» на первом голосовании, чтобы отражать
    актуальное владение на момент волеизъявления. comments — необязательное
    текстовое обоснование по каждому вопросу (VoteBallot.comment, ПУБЛИЧНОЕ
    — см. models.VoteBallot); отсутствие ключа или пустая строка — без
    комментария. Не коммитит сама.
    """
    weight = person_voting_weight(person.id)
    comments = comments or {}
    now = dt.datetime.now()
    question_ids = {q.id: q for q in vote.questions}
    for question_id, choice in choices.items():
        if question_id not in question_ids:
            continue
        comment = (comments.get(question_id) or "").strip() or None
        existing = (
            database.db_session.query(VoteBallot)
            .filter_by(question_id=question_id, person_id=person.id)
            .first()
        )
        if existing is not None:
            existing.choice = choice
            existing.weight = weight
            existing.comment = comment
            existing.cast_at = now
        else:
            database.db_session.add(VoteBallot(
                question_id=question_id, person_id=person.id, choice=choice, weight=weight,
                comment=comment, cast_at=now,
            ))


# ---------------------------------------------------------------------------
# Роуты — правление
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def list_votes():
    query = database.db_session.query(Vote).order_by(Vote.opens_at.desc())
    if not is_board():
        # рядовые члены не видят голосования, которые ещё формируются
        query = query.filter(Vote.status != VoteStatus.DRAFT)
    votes = query.all()

    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    my_weight = person_voting_weight(person.id) if person else Decimal("0")
    voted_question_ids = set()
    if person:
        for vote in votes:
            voted_question_ids |= set(person_ballots_by_question(vote, person.id).keys())

    return render_template(
        "voting/list.html", votes=votes, quorum_met=quorum_met,
        is_accepting_ballots=is_accepting_ballots, my_weight=my_weight,
        voted_question_ids=voted_question_ids,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def create():
    if request.method == "POST":
        f = request.form
        voting_type = VoteType(f.get("voting_type") or VoteType.ABSENTEE.value)

        if voting_type == VoteType.IN_PERSON:
            # Полностью очное голосование — решение уже принято на собрании,
            # электронной повестки/бюллетеней не заводим. Протокол
            # обязателен сразу при создании (без него нечем подтвердить
            # результат), и Vote создаётся сразу закрытым — открывать/
            # переголосовывать здесь нечего.
            vote_date_raw = f.get("vote_date")
            if not vote_date_raw:
                flash(_("Укажите дату голосования."), "danger")
                return redirect(url_for("voting.create"))
            vote_date = dt.datetime.fromisoformat(vote_date_raw)

            file_path = save_upload(request.files.get("protocol_file"), current_app.config["UPLOAD_FOLDER"])
            if not file_path:
                flash(_("Для очного голосования обязательно приложите протокол с результатами."), "danger")
                return redirect(url_for("voting.create"))

            vote = Vote(
                title=f["title"],
                description=f.get("description") or None,
                voting_type=voting_type,
                meeting_id=int(f["meeting_id"]) if f.get("meeting_id") else None,
                opens_at=vote_date, closes_at=vote_date, closed_at=dt.datetime.now(),
                created_by_person_id=g.user.person_id,
                status=VoteStatus.CLOSED,
            )
            database.db_session.add(vote)
            database.db_session.flush()

            doc = Document(
                doc_type=DocumentType.PROTOCOL,
                date=vote_date.date(),
                title=_("Протокол голосования «{title}»", title=vote.title),
                file_path=file_path,
            )
            database.db_session.add(doc)
            database.db_session.flush()
            vote.protocol_document_id = doc.id
            audit.record("vote.create", f"Зафиксировано очное голосование «{vote.title}» от {audit.format_date(vote_date.date())}, протокол прикреплён")
            database.db_session.commit()
            flash(_("Очное голосование зафиксировано, протокол прикреплён."), "success")
            return redirect(url_for("voting.detail", vote_id=vote.id))

        opens_at = dt.datetime.fromisoformat(f["opens_at"])
        closes_at = dt.datetime.fromisoformat(f["closes_at"])
        if closes_at <= opens_at:
            flash(_("Дата окончания должна быть позже даты начала."), "danger")
            return redirect(url_for("voting.create"))

        vote = Vote(
            title=f["title"],
            description=f.get("description") or None,
            voting_type=voting_type,
            meeting_id=int(f["meeting_id"]) if f.get("meeting_id") else None,
            opens_at=opens_at, closes_at=closes_at,
            created_by_person_id=g.user.person_id,
            status=VoteStatus.DRAFT,
        )
        database.db_session.add(vote)
        audit.record("vote.create", f"Создано голосование-черновик «{vote.title}» ({voting_type.value})")
        database.db_session.commit()
        flash(_("Голосование создано (черновик) — теперь добавьте вопросы повестки."), "success")
        return redirect(url_for("voting.detail", vote_id=vote.id))

    meetings = database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).all()
    now_local = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
    return render_template("voting/form.html", meetings=meetings, now_local=now_local, vote=None)


@bp.route("/<int:vote_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit(vote_id):
    """
    Правка голосования доступна, только пока оно черновик (status ==
    DRAFT) — после открытия менять тип/сроки нельзя, т.к. это исказило бы
    уже поданные бюллетени и веса кворума. Очные (IN_PERSON) голосования
    создаются сразу закрытыми (см. create()) и в черновик никогда не
    попадают, так что сюда не доходят — тип голосования тут можно менять
    только между «заочное» и «очно-заочное».
    """
    vote = database.db_session.get(Vote, vote_id)
    if vote is None:
        flash(_("Голосование не найдено."), "warning")
        return redirect(url_for("voting.list_votes"))
    if vote.status != VoteStatus.DRAFT:
        flash(_("Редактировать можно только голосование-черновик."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    if request.method == "POST":
        f = request.form
        voting_type = VoteType(f.get("voting_type") or VoteType.ABSENTEE.value)
        if voting_type == VoteType.IN_PERSON:
            flash(_("Нельзя изменить тип на очный для уже созданного голосования."), "danger")
            return redirect(url_for("voting.edit", vote_id=vote_id))

        opens_at = dt.datetime.fromisoformat(f["opens_at"])
        closes_at = dt.datetime.fromisoformat(f["closes_at"])
        if closes_at <= opens_at:
            flash(_("Дата окончания должна быть позже даты начала."), "danger")
            return redirect(url_for("voting.edit", vote_id=vote_id))

        vote.title = f["title"]
        vote.description = f.get("description") or None
        vote.voting_type = voting_type
        vote.meeting_id = int(f["meeting_id"]) if f.get("meeting_id") else None
        vote.opens_at = opens_at
        vote.closes_at = closes_at
        audit.record("vote.edit", f"Изменено голосование-черновик «{vote.title}»", entity_type="vote", entity_id=vote.id)
        database.db_session.commit()
        flash(_("Голосование обновлено."), "success")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    meetings = database.db_session.query(GeneralMeeting).order_by(GeneralMeeting.date.desc()).all()
    now_local = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
    return render_template("voting/form.html", meetings=meetings, now_local=now_local, vote=vote)


@bp.route("/<int:vote_id>")
@login_required
def detail(vote_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None:
        flash(_("Голосование не найдено."), "warning")
        return redirect(url_for("voting.list_votes"))
    if vote.status == VoteStatus.DRAFT and not is_board():
        flash(_("Это голосование ещё не открыто."), "warning")
        return redirect(url_for("voting.list_votes"))

    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    my_weight = person_voting_weight(person.id) if person else Decimal("0")
    my_ballots = person_ballots_by_question(vote, person.id) if person else {}

    results = {q.id: question_results(q) for q in vote.questions} if (is_board() or vote.status == VoteStatus.CLOSED) else {}

    # Ручная запись очных голосов — только председатель, только для
    # очно-заочного голосования, пока оно открыто (см. set_ballot_for_person).
    show_manual_ballots = is_chairman() and vote.voting_type == VoteType.IN_PERSON_AND_ABSENTEE and vote.status == VoteStatus.OPEN
    voters = eligible_voters() if show_manual_ballots else []
    voter_ballots = {p.id: person_ballots_by_question(vote, p.id) for p in voters} if show_manual_ballots else {}
    voter_weights = {p.id: person_voting_weight(p.id) for p in voters} if show_manual_ballots else {}

    return render_template(
        "voting/detail.html", vote=vote, results=results,
        my_weight=my_weight, my_ballots=my_ballots,
        is_accepting=is_accepting_ballots(vote),
        quorum=quorum_met(vote), participation=vote_participation_weight(vote) if is_board() else None,
        total_weight=total_cooperative_weight(),
        show_manual_ballots=show_manual_ballots, voters=voters, voter_ballots=voter_ballots, voter_weights=voter_weights,
    )


@bp.route("/<int:vote_id>/questions/add", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def add_question(vote_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None or vote.status != VoteStatus.DRAFT:
        flash(_("Добавлять вопросы можно только пока голосование не открыто."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    f = request.form
    max_order = max((q.order for q in vote.questions), default=-1)
    database.db_session.add(VoteQuestion(
        vote_id=vote.id, order=max_order + 1, text=f["text"],
        majority_threshold=parse_decimal(f.get("majority_threshold") or "0.5"),
    ))
    audit.record("vote.question_add", f"В повестку голосования «{vote.title}» добавлен вопрос: {f['text']}", entity_type="vote", entity_id=vote.id)
    database.db_session.commit()
    flash(_("Вопрос добавлен."), "success")
    return redirect(url_for("voting.detail", vote_id=vote_id))


@bp.route("/<int:vote_id>/questions/<int:question_id>/delete", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def delete_question(vote_id, question_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None or vote.status != VoteStatus.DRAFT:
        flash(_("Удалять вопросы можно только пока голосование не открыто."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))
    question = database.db_session.get(VoteQuestion, question_id)
    if question is not None and question.vote_id == vote_id:
        audit.record("vote.question_delete", f"Из повестки голосования «{vote.title}» удалён вопрос: {question.text}", entity_type="vote", entity_id=vote.id)
        database.db_session.delete(question)
        database.db_session.commit()
        flash(_("Вопрос удалён."), "success")
    return redirect(url_for("voting.detail", vote_id=vote_id))


@bp.route("/<int:vote_id>/open", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def open_vote(vote_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None or vote.status != VoteStatus.DRAFT:
        flash(_("Голосование уже открыто или закрыто."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))
    if not vote.questions:
        flash(_("Нельзя открыть голосование без вопросов повестки."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))
    vote.status = VoteStatus.OPEN
    audit.record("vote.open", f"Открыто голосование «{vote.title}»", entity_type="vote", entity_id=vote.id)
    database.db_session.commit()
    flash(_("Голосование открыто — члены кооператива теперь могут голосовать."), "success")
    return redirect(url_for("voting.detail", vote_id=vote_id))


@bp.route("/<int:vote_id>/close", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def close_vote(vote_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None or vote.status != VoteStatus.OPEN:
        flash(_("Закрыть можно только открытое голосование."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))
    vote.status = VoteStatus.CLOSED
    vote.closed_at = dt.datetime.now()
    audit.record("vote.close", f"Закрыто голосование «{vote.title}», результаты зафиксированы", entity_type="vote", entity_id=vote.id)
    database.db_session.commit()
    flash(_("Голосование закрыто, результаты зафиксированы."), "success")
    return redirect(url_for("voting.detail", vote_id=vote_id))


@bp.route("/<int:vote_id>/protocol", methods=["POST"])
@roles_required(RoleEnum.CHAIRMAN)
def attach_protocol(vote_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None:
        flash(_("Голосование не найдено."), "warning")
        return redirect(url_for("voting.list_votes"))

    file_path = save_upload(request.files.get("protocol_file"), current_app.config["UPLOAD_FOLDER"])
    if not file_path:
        flash(_("Не удалось сохранить файл протокола."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    doc = Document(
        doc_type=DocumentType.PROTOCOL,
        date=dt.date.today(),
        title=_("Протокол голосования «{title}»", title=vote.title),
        file_path=file_path,
    )
    database.db_session.add(doc)
    database.db_session.flush()
    vote.protocol_document_id = doc.id
    audit.record("vote.protocol_attach", f"К голосованию «{vote.title}» прикреплён протокол", entity_type="vote", entity_id=vote.id)
    database.db_session.commit()
    flash(_("Протокол прикреплён."), "success")
    return redirect(url_for("voting.detail", vote_id=vote_id))


# ---------------------------------------------------------------------------
# Роуты — голосование члена кооператива
# ---------------------------------------------------------------------------

@bp.route("/<int:vote_id>/ballot", methods=["GET", "POST"])
@login_required
def ballot(vote_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None:
        flash(_("Голосование не найдено."), "warning")
        return redirect(url_for("voting.list_votes"))

    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    if person is None:
        flash(_("Ваша учётная запись не привязана к карточке члена кооператива — обратитесь в правление."), "warning")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    weight = person_voting_weight(person.id)
    if weight <= 0:
        flash(_("Голосовать могут только собственники гаражей — у вас нет доли ни в одном гараже."), "warning")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    if not is_accepting_ballots(vote):
        flash(_("Приём бюллетеней по этому голосованию сейчас закрыт."), "warning")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    if request.method == "POST":
        f = request.form
        choices = {}
        comments = {}
        for question in vote.questions:
            raw = f.get(f"choice_{question.id}")
            if raw:
                choices[question.id] = VoteChoice(raw)
                comments[question.id] = f.get(f"comment_{question.id}", "")
        if len(choices) != len(vote.questions):
            flash(_("Ответьте на все вопросы повестки перед отправкой бюллетеня."), "danger")
            return redirect(url_for("voting.ballot", vote_id=vote_id))
        cast_ballots(vote, person, choices, comments)
        database.db_session.commit()
        flash(_("Бюллетень принят. Вы можете изменить голос, пока приём бюллетеней открыт."), "success")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    my_ballots = person_ballots_by_question(vote, person.id)
    return render_template("voting/ballot.html", vote=vote, weight=weight, my_ballots=my_ballots)


# ---------------------------------------------------------------------------
# Роут — председатель фиксирует, как проголосовал конкретный член
# кооператива (для очно-заочного: часть голосов подаётся очно на
# собрании, на бумаге, и в систему сама не попадает)
# ---------------------------------------------------------------------------

@bp.route("/<int:vote_id>/ballot/<int:person_id>", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def set_ballot_for_person(vote_id, person_id):
    vote = database.db_session.get(Vote, vote_id)
    if vote is None:
        flash(_("Голосование не найдено."), "warning")
        return redirect(url_for("voting.list_votes"))

    if vote.voting_type != VoteType.IN_PERSON_AND_ABSENTEE:
        flash(_("Ручная запись голоса доступна только для очно-заочного голосования."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))
    if vote.status != VoteStatus.OPEN:
        flash(_("Записывать голоса можно только пока голосование открыто."), "danger")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    person = database.db_session.get(Person, person_id)
    if person is None:
        flash(_("Человек не найден."), "warning")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    weight = person_voting_weight(person.id)
    if weight <= 0:
        flash(_("У этого человека нет доли ни в одном гараже — голосовать он не может."), "warning")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    if request.method == "POST":
        f = request.form
        choices = {}
        comments = {}
        for question in vote.questions:
            raw = f.get(f"choice_{question.id}")
            if raw:
                choices[question.id] = VoteChoice(raw)
                comments[question.id] = f.get(f"comment_{question.id}", "")
        if not choices:
            flash(_("Отметьте хотя бы один вопрос повестки."), "danger")
            return redirect(url_for("voting.set_ballot_for_person", vote_id=vote_id, person_id=person_id))
        cast_ballots(vote, person, choices, comments)
        audit.record(
            "vote.manual_ballot",
            f"Председатель вручную зафиксировал голос «{person.full_name}» в голосовании «{vote.title}»",
            entity_type="person", entity_id=person.id,
        )
        database.db_session.commit()
        flash(_("Голос члена кооператива «{name}» зафиксирован.", name=person.full_name), "success")
        return redirect(url_for("voting.detail", vote_id=vote_id))

    ballots = person_ballots_by_question(vote, person.id)
    return render_template(
        "voting/ballot.html", vote=vote, weight=weight, my_ballots=ballots,
        on_behalf_of=person,
    )
