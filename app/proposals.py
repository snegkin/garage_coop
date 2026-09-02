"""
Предложения голосований от рядовых членов кооператива — канал вынести
вопрос на общее голосование, не будучи лично в правлении.

Организационная модель:
- Любой член кооператива (собственник хотя бы доли гаража) подаёт
  предложение — тему и описание (create()). Статус — PENDING.
- Правление рассматривает предложение голосованием «за/против/воздержался»
  вынесения его на общее голосование (board_vote()) — по головам, один
  голос на члена текущего созыва правления (не по долям владения, в
  отличие от самого Vote); «воздержался» не засчитывается ни в чью пользу.
- Пока предложение PENDING, председатель может поправить формулировку
  (edit()).
- Решение подводится (resolve_if_due()), как только выполняется одно из
  двух условий: проголосовали ВСЕ члены текущего созыва правления, либо
  истёк PROPOSAL_REVIEW_PERIOD (неделя) с момента подачи. Голосов «за»
  больше, чем «против» -> APPROVED, и тут же создаётся Vote-черновик с тем
  же названием/описанием (председателю остаётся сформировать повестку и
  открыть его — обычный путь voting.py); иначе -> REJECTED. Ничья (в т.ч.
  0:0, если никто из правления не успел проголосовать за неделю) считается
  отклонением — правление не набрало большинства «за».
- Резолюция вычисляется лениво при обращении к предложению (список,
  карточка, сразу после голосования члена правления) — тот же приём, что у
  is_accepting_ballots в voting.py, без отдельного планировщика/cron.
"""
import datetime as dt

from flask import Blueprint, render_template, request, redirect, url_for, flash, g

from . import database
from .i18n import translate as _
from .auth import login_required, roles_required
from .governance import current_board_member_ids
from .voting import person_voting_weight
from .models import (
    VoteProposal, VoteProposalBoardBallot, ProposalStatus, PROPOSAL_REVIEW_PERIOD,
    Vote, VoteType, VoteStatus, VoteChoice, Person, RoleEnum,
)

bp = Blueprint("proposals", __name__, url_prefix="/proposals")


# ---------------------------------------------------------------------------
# Логика решения правления
# ---------------------------------------------------------------------------

def _board_ballots(proposal: VoteProposal) -> list[VoteProposalBoardBallot]:
    """Прямой запрос вместо relationship — актуален даже сразу после добавления своего бюллетеня в этой же транзакции (см. board_vote)."""
    return (
        database.db_session.query(VoteProposalBoardBallot)
        .filter_by(proposal_id=proposal.id)
        .all()
    )


def proposal_tally(proposal: VoteProposal) -> dict:
    """
    Голоса «за»/«против»/«воздержался» среди ЧЛЕНОВ ТЕКУЩЕГО состава
    правления — если состав сменился, голос выбывшего в подсчёт не идёт.
    «Воздержался» не влияет на решение ни в чью пользу (см.
    resolve_if_due: сравниваются только for/against) — как и в обычном
    голосовании (voting.py), это способ отметиться проголосовавшим, не
    поддерживая ни одну из сторон.
    """
    board_ids = current_board_member_ids()
    ballots = [b for b in _board_ballots(proposal) if b.person_id in board_ids]
    for_count = sum(1 for b in ballots if b.choice == VoteChoice.FOR)
    against_count = sum(1 for b in ballots if b.choice == VoteChoice.AGAINST)
    abstain_count = sum(1 for b in ballots if b.choice == VoteChoice.ABSTAIN)
    return {
        "board_ids": board_ids, "voted_ids": {b.person_id for b in ballots},
        "for": for_count, "against": against_count, "abstain": abstain_count,
    }


def resolve_if_due(proposal: VoteProposal) -> bool:
    """
    Если предложение ещё PENDING и наступило условие решения (все члены
    текущего созыва правления проголосовали, либо истёк
    PROPOSAL_REVIEW_PERIOD с created_at) — подводит итог и, при одобрении,
    сразу создаёт Vote-черновик. Не коммитит сама (как и voting.cast_ballots)
    — коммит на вызывающей стороне, в одной транзакции с самим действием.
    Возвращает True, если решение было принято прямо сейчас.
    """
    if proposal.status != ProposalStatus.PENDING:
        return False

    tally = proposal_tally(proposal)
    now = dt.datetime.now()
    all_voted = bool(tally["board_ids"]) and tally["board_ids"] <= tally["voted_ids"]
    deadline_passed = now >= proposal.created_at + PROPOSAL_REVIEW_PERIOD
    if not (all_voted or deadline_passed):
        return False

    proposal.decided_at = now
    if tally["for"] > tally["against"]:
        proposal.status = ProposalStatus.APPROVED
        vote = Vote(
            title=proposal.title,
            description=proposal.description,
            voting_type=VoteType.ABSENTEE,
            status=VoteStatus.DRAFT,
            opens_at=now,
            closes_at=now + dt.timedelta(days=14),
            created_by_person_id=proposal.proposed_by_person_id,
        )
        database.db_session.add(vote)
        database.db_session.flush()
        proposal.resulting_vote_id = vote.id
    else:
        proposal.status = ProposalStatus.REJECTED
    return True


def _resolve_all_pending() -> bool:
    """Прогоняет resolve_if_due по всем PENDING-предложениям — вызывается со страниц списка/карточки, чтобы просроченные по неделе решались без отдельного действия председателя."""
    pending = database.db_session.query(VoteProposal).filter_by(status=ProposalStatus.PENDING).all()
    return any(resolve_if_due(p) for p in pending)


# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def list_proposals():
    if _resolve_all_pending():
        database.db_session.commit()

    proposals = database.db_session.query(VoteProposal).order_by(VoteProposal.created_at.desc()).all()
    board_ids = current_board_member_ids()
    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    my_ballots = {}
    if person:
        for p in proposals:
            ballot = next((b for b in _board_ballots(p) if b.person_id == person.id), None)
            if ballot:
                my_ballots[p.id] = ballot

    deadlines = {p.id: p.created_at + PROPOSAL_REVIEW_PERIOD for p in proposals}

    return render_template(
        "proposals/list.html", proposals=proposals, board_ids=board_ids,
        my_ballots=my_ballots, can_propose=bool(person) and person_voting_weight(person.id) > 0,
        deadlines=deadlines,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    if person is None or person_voting_weight(person.id) <= 0:
        flash(_("Предлагать голосование могут только собственники гаражей — члены кооператива."), "warning")
        return redirect(url_for("proposals.list_proposals"))

    if request.method == "POST":
        f = request.form
        title = (f.get("title") or "").strip()
        if not title:
            flash(_("Укажите тему голосования."), "danger")
            return redirect(url_for("proposals.create"))

        proposal = VoteProposal(
            title=title,
            description=f.get("description") or None,
            proposed_by_person_id=person.id,
            created_at=dt.datetime.now(),
        )
        database.db_session.add(proposal)
        database.db_session.commit()
        flash(_("Предложение отправлено правлению на рассмотрение."), "success")
        return redirect(url_for("proposals.detail", proposal_id=proposal.id))

    return render_template("proposals/form.html", proposal=None)


@bp.route("/<int:proposal_id>/edit", methods=["GET", "POST"])
@roles_required(RoleEnum.CHAIRMAN)
def edit(proposal_id):
    proposal = database.db_session.get(VoteProposal, proposal_id)
    if proposal is None:
        flash(_("Предложение не найдено."), "warning")
        return redirect(url_for("proposals.list_proposals"))
    if proposal.status != ProposalStatus.PENDING:
        flash(_("Редактировать можно только предложение, ожидающее решения правления."), "danger")
        return redirect(url_for("proposals.detail", proposal_id=proposal_id))

    if request.method == "POST":
        f = request.form
        title = (f.get("title") or "").strip()
        if not title:
            flash(_("Укажите тему голосования."), "danger")
            return redirect(url_for("proposals.edit", proposal_id=proposal_id))
        proposal.title = title
        proposal.description = f.get("description") or None
        database.db_session.commit()
        flash(_("Предложение обновлено."), "success")
        return redirect(url_for("proposals.detail", proposal_id=proposal_id))

    return render_template("proposals/form.html", proposal=proposal)


@bp.route("/<int:proposal_id>")
@login_required
def detail(proposal_id):
    proposal = database.db_session.get(VoteProposal, proposal_id)
    if proposal is None:
        flash(_("Предложение не найдено."), "warning")
        return redirect(url_for("proposals.list_proposals"))

    if resolve_if_due(proposal):
        database.db_session.commit()

    tally = proposal_tally(proposal)
    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    my_ballot = None
    if person:
        my_ballot = next((b for b in _board_ballots(proposal) if b.person_id == person.id), None)
    can_vote = bool(person) and person.id in tally["board_ids"] and proposal.status == ProposalStatus.PENDING

    return render_template(
        "proposals/detail.html", proposal=proposal, tally=tally, my_ballot=my_ballot,
        can_vote=can_vote, deadline=proposal.created_at + PROPOSAL_REVIEW_PERIOD,
    )


@bp.route("/<int:proposal_id>/board-vote", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def board_vote(proposal_id):
    proposal = database.db_session.get(VoteProposal, proposal_id)
    if proposal is None:
        flash(_("Предложение не найдено."), "warning")
        return redirect(url_for("proposals.list_proposals"))

    person = database.db_session.get(Person, g.user.person_id) if g.user.person_id else None
    if person is None or person.id not in current_board_member_ids():
        flash(_("Голосовать за одобрение предложений может только член текущего созыва правления."), "danger")
        return redirect(url_for("proposals.detail", proposal_id=proposal_id))

    if proposal.status != ProposalStatus.PENDING:
        flash(_("Решение по этому предложению уже принято."), "warning")
        return redirect(url_for("proposals.detail", proposal_id=proposal_id))

    raw = request.form.get("choice")
    if raw not in (VoteChoice.FOR.value, VoteChoice.AGAINST.value, VoteChoice.ABSTAIN.value):
        flash(_("Выберите «за», «против» или «воздержался»."), "danger")
        return redirect(url_for("proposals.detail", proposal_id=proposal_id))
    choice = VoteChoice(raw)
    comment = (request.form.get("comment") or "").strip() or None

    existing = (
        database.db_session.query(VoteProposalBoardBallot)
        .filter_by(proposal_id=proposal.id, person_id=person.id)
        .first()
    )
    now = dt.datetime.now()
    if existing is not None:
        existing.choice = choice
        existing.comment = comment
        existing.voted_at = now
    else:
        database.db_session.add(VoteProposalBoardBallot(
            proposal_id=proposal.id, person_id=person.id, choice=choice, comment=comment, voted_at=now,
        ))
    database.db_session.flush()

    resolve_if_due(proposal)
    database.db_session.commit()
    flash(_("Ваш голос учтён."), "success")
    return redirect(url_for("proposals.detail", proposal_id=proposal_id))
