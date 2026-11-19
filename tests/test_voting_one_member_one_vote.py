"""
Режим «один член — один голос» (Cooperative.one_member_one_vote → Vote.
one_member_one_vote при открытии): по умолчанию выключен — вес по долям,
как раньше; включённый — у каждого собственника вес 1. Режим фиксируется в
голосовании при открытии и входит в его genesis-хеш.
"""
import datetime as dt
from decimal import Decimal

from app import notifications, vote_ledger
from app.models import Cooperative, Vote, VoteChoice, VoteQuestion, VoteStatus, VoteType, RoleEnum
from app.voting import person_voting_weight, quorum_met, question_results, total_cooperative_weight

from tests.conftest import login, make_garage, make_ownership, make_person, make_user


def _setup(db, client, monkeypatch, *, per_member: bool):
    """У Анны три гаража, у Бориса и Веры — по гаражу; голосование открыто через роут."""
    monkeypatch.setattr(notifications, "notify_persons", lambda *a, **k: None)
    db.add(Cooperative(full_name="ГМ", inn="1", kpp="1", ogrn="1", one_member_one_vote=per_member))
    anna, boris, vera = (make_person(db, full_name=n) for n in ("Анна", "Борис", "Вера"))
    for i in range(3):
        make_ownership(db, make_garage(db, number=f"A{i}"), anna)
    make_ownership(db, make_garage(db, number="B"), boris)
    make_ownership(db, make_garage(db, number="V"), vera)
    make_user(db, "chair", "pw", role=RoleEnum.CHAIRMAN, person=anna)
    now = dt.datetime.now()
    vote = Vote(title="Выборы", voting_type=VoteType.ABSENTEE, status=VoteStatus.DRAFT,
                opens_at=now - dt.timedelta(hours=1), closes_at=now + dt.timedelta(days=1))
    db.add(vote)
    db.flush()
    q = VoteQuestion(vote_id=vote.id, text="Избрать?", majority_threshold=Decimal("0.5"))
    db.add(q)
    db.commit()
    login(client, "chair", "pw")
    client.post(f"/voting/{vote.id}/open")
    db.expire_all()
    return db.get(Vote, vote.id), db.get(VoteQuestion, q.id), anna, boris, vera


def test_default_is_share_weighted(app, db, client, monkeypatch):
    vote, q, anna, boris, vera = _setup(db, client, monkeypatch, per_member=False)
    assert vote.one_member_one_vote is False
    assert person_voting_weight(anna.id, vote) == Decimal("3")
    assert total_cooperative_weight(vote) == Decimal("5")


def test_one_member_one_vote_changes_outcome(app, db, client, monkeypatch):
    vote, q, anna, boris, vera = _setup(db, client, monkeypatch, per_member=True)
    assert vote.one_member_one_vote is True
    assert person_voting_weight(anna.id, vote) == Decimal("1")
    assert total_cooperative_weight(vote) == Decimal("3")

    client.post(f"/voting/{vote.id}/ballot", data={f"choice_{q.id}": "for"})  # Анна
    from app.voting import cast_ballots
    cast_ballots(vote, boris, {q.id: VoteChoice.AGAINST})
    cast_ballots(vote, vera, {q.id: VoteChoice.AGAINST})
    db.commit()
    db.expire_all()
    vote = db.get(Vote, vote.id)
    assert quorum_met(vote)
    res = question_results(db.get(VoteQuestion, q.id))
    # По долям Анна (3 из 5) победила бы; по головам — 1 против 2.
    assert res["for"] == Decimal("1") and res["against"] == Decimal("2")
    assert not res["passed"]

    client.post(f"/voting/{vote.id}/close")
    db.expire_all()
    vote = db.get(Vote, vote.id)
    assert vote.final_total_weight == Decimal("3")
    assert vote_ledger.verify(vote).ok


def test_mode_fixed_at_open_and_in_hash(app, db, client, monkeypatch):
    vote, q, anna, boris, vera = _setup(db, client, monkeypatch, per_member=True)
    # Настройку выключили, пока голосование идёт — веса не меняются.
    db.query(Cooperative).one().one_member_one_vote = False
    db.commit()
    assert person_voting_weight(anna.id, vote) == Decimal("1")
    assert vote_ledger.verify(vote).ok

    # Подмена режима в самом голосовании видна по genesis-хешу.
    vote.one_member_one_vote = False
    db.commit()
    assert not vote_ledger.verify(vote).ok


def test_stuffing_co_owners_into_one_garage_gives_no_extra_votes(app, db, client, monkeypatch):
    """Сотня «совладельцев» одного гаража в сумме — всё равно один голос."""
    monkeypatch.setattr(notifications, "notify_persons", lambda *a, **k: None)
    db.add(Cooperative(full_name="ГМ", inn="1", kpp="1", ogrn="1", one_member_one_vote=True))
    honest = make_person(db, full_name="Честный")
    make_ownership(db, make_garage(db, number="H"), honest)
    stuffed = make_garage(db, number="S")
    fakes = [make_person(db, full_name=f"Подставной {i}") for i in range(100)]
    for p in fakes:
        make_ownership(db, stuffed, p, share="0.01")
    db.commit()

    assert person_voting_weight(honest.id) == Decimal("1")
    assert person_voting_weight(fakes[0].id) == Decimal("0.01")
    assert total_cooperative_weight() == Decimal("2")
