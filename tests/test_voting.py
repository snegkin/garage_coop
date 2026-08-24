"""
Тесты на вес голоса по доле владения, кворум (строго >50%) и порог принятия
решения — это правила, ошибка в которых может сделать нелегитимным
реальное решение собрания кооператива, поэтому закрепляем их тестами.
"""
import datetime as dt
from decimal import Decimal

from app.voting import (
    person_voting_weight, total_cooperative_weight, quorum_met,
    question_results, cast_ballots,
)
from app.models import Vote, VoteQuestion, VoteType, VoteStatus, VoteChoice

from tests.conftest import make_garage, make_person, make_ownership


def _make_vote(db, status=VoteStatus.OPEN):
    now = dt.datetime.now()
    vote = Vote(
        title="Тестовое голосование",
        voting_type=VoteType.ABSENTEE,
        status=status,
        opens_at=now - dt.timedelta(days=1),
        closes_at=now + dt.timedelta(days=1),
    )
    db.add(vote)
    db.flush()
    question = VoteQuestion(vote_id=vote.id, text="Одобрить смету?", majority_threshold=Decimal("0.5"))
    db.add(question)
    db.flush()
    return vote, question


def test_voting_weight_equals_sum_of_shares(app, db):
    person = make_person(db)
    garage1 = make_garage(db, number="1")
    garage2 = make_garage(db, number="2")
    make_ownership(db, garage1, person, share="1")
    make_ownership(db, garage2, person, share="0.5")
    db.commit()

    assert person_voting_weight(person.id) == Decimal("1.5")


def test_co_owner_weight_is_proportional_to_share(app, db):
    garage = make_garage(db)
    owner_a = make_person(db, full_name="A")
    owner_b = make_person(db, full_name="B")
    make_ownership(db, garage, owner_a, share="0.7")
    make_ownership(db, garage, owner_b, share="0.3")
    db.commit()

    assert person_voting_weight(owner_a.id) == Decimal("0.7")
    assert person_voting_weight(owner_b.id) == Decimal("0.3")
    assert total_cooperative_weight() == Decimal("1.0")


def test_quorum_not_met_below_half(app, db):
    garage1 = make_garage(db, number="1")
    garage2 = make_garage(db, number="2")
    voter = make_person(db, full_name="Voter")
    silent = make_person(db, full_name="Silent")
    make_ownership(db, garage1, voter, share="1")
    make_ownership(db, garage2, silent, share="1")
    db.commit()

    vote, question = _make_vote(db)
    cast_ballots(vote, voter, {question.id: VoteChoice.FOR})
    db.commit()

    # Проголосовал только один из двух гаражей (50% ровно) — по правилу
    # нужно СТРОГО больше половины, значит кворума нет.
    assert quorum_met(vote) is False


def test_quorum_met_strictly_above_half(app, db):
    garage1 = make_garage(db, number="1")
    garage2 = make_garage(db, number="2")
    garage3 = make_garage(db, number="3")
    voter1 = make_person(db, full_name="V1")
    voter2 = make_person(db, full_name="V2")
    silent = make_person(db, full_name="Silent")
    make_ownership(db, garage1, voter1, share="1")
    make_ownership(db, garage2, voter2, share="1")
    make_ownership(db, garage3, silent, share="1")
    db.commit()

    vote, question = _make_vote(db)
    cast_ballots(vote, voter1, {question.id: VoteChoice.FOR})
    cast_ballots(vote, voter2, {question.id: VoteChoice.AGAINST})
    db.commit()

    # 2 из 3 гаражей проголосовали — 2/3 > 50%.
    assert quorum_met(vote) is True


def test_question_fails_without_quorum_even_if_for_wins_locally(app, db):
    """Даже если все проголосовавшие — "за", решение не проходит без кворума
    по кооперативу в целом (см. docstring question_results)."""
    garage1 = make_garage(db, number="1")
    garage2 = make_garage(db, number="2")
    garage3 = make_garage(db, number="3")
    voter = make_person(db, full_name="V1")
    silent1 = make_person(db, full_name="S1")
    silent2 = make_person(db, full_name="S2")
    make_ownership(db, garage1, voter, share="1")
    make_ownership(db, garage2, silent1, share="1")
    make_ownership(db, garage3, silent2, share="1")
    db.commit()

    vote, question = _make_vote(db)
    cast_ballots(vote, voter, {question.id: VoteChoice.FOR})
    db.commit()

    results = question_results(question)
    assert results["for"] == Decimal("1")
    assert quorum_met(vote) is False
    assert results["passed"] is False


def test_question_passes_with_quorum_and_majority(app, db):
    garage1 = make_garage(db, number="1")
    garage2 = make_garage(db, number="2")
    voter1 = make_person(db, full_name="V1")
    voter2 = make_person(db, full_name="V2")
    make_ownership(db, garage1, voter1, share="1")
    make_ownership(db, garage2, voter2, share="1")
    db.commit()

    vote, question = _make_vote(db)
    cast_ballots(vote, voter1, {question.id: VoteChoice.FOR})
    cast_ballots(vote, voter2, {question.id: VoteChoice.FOR})
    db.commit()

    results = question_results(question)
    assert quorum_met(vote) is True
    assert results["passed"] is True


def test_revote_updates_existing_ballot_not_duplicate(app, db):
    """Переголосование, пока голосование открыто, должно обновлять тот же
    бюллетень (upsert), а не создавать второй — иначе вес человека
    задвоится в подсчёте."""
    garage = make_garage(db)
    voter = make_person(db)
    make_ownership(db, garage, voter, share="1")
    db.commit()

    vote, question = _make_vote(db)
    cast_ballots(vote, voter, {question.id: VoteChoice.AGAINST})
    db.commit()
    cast_ballots(vote, voter, {question.id: VoteChoice.FOR})
    db.commit()

    assert len(question.ballots) == 1
    assert question.ballots[0].choice == VoteChoice.FOR

    results = question_results(question)
    assert results["for"] == Decimal("1")
    assert results["against"] == Decimal("0")
