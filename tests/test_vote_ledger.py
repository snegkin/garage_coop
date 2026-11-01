"""
Журнал бюллетеней (hash-цепочка, app/vote_ledger.py): любая подмена голосов,
повестки или итогов задним числом должна всплывать в verify(), а обычная
работа (голос, переголосование, ручная запись председателем) — нет.
"""
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import notifications, vote_ledger
from app.models import (
    Vote, VoteQuestion, VoteType, VoteStatus, VoteChoice, VoteBallot, VoteBallotEvent, RoleEnum,
)
from app.voting import cast_ballots, question_results

from tests.conftest import make_garage, make_person, make_ownership, make_user, login


def _setup(db, voting_type=VoteType.ABSENTEE):
    """Два собственника по гаражу, голосование с двумя вопросами, уже открытое (с журналом)."""
    a = make_person(db, full_name="Анна")
    b = make_person(db, full_name="Борис")
    make_ownership(db, make_garage(db, number="1"), a, share="1")
    make_ownership(db, make_garage(db, number="2"), b, share="1")
    now = dt.datetime.now()
    vote = Vote(
        title="Смета", voting_type=voting_type, status=VoteStatus.DRAFT,
        opens_at=now - dt.timedelta(days=1), closes_at=now + dt.timedelta(days=1),
    )
    db.add(vote)
    db.flush()
    q1 = VoteQuestion(vote_id=vote.id, order=1, text="Утвердить смету?", majority_threshold=Decimal("0.5"))
    q2 = VoteQuestion(vote_id=vote.id, order=2, text="Переизбрать правление?", majority_threshold=Decimal("0.6667"))
    db.add_all([q1, q2])
    db.flush()
    vote.status = VoteStatus.OPEN
    vote_ledger.seal_on_open(vote)
    db.commit()
    return vote, q1, q2, a, b


def _close(db, vote):
    vote.status = VoteStatus.CLOSED
    vote.closed_at = dt.datetime.now()
    vote_ledger.seal_on_close(vote, Decimal("2"))
    db.commit()


def _events(db, vote):
    return db.query(VoteBallotEvent).filter_by(vote_id=vote.id).order_by(VoteBallotEvent.seq).all()


def test_chain_grows_and_verifies(app, db):
    vote, q1, q2, a, b = _setup(db)
    assert len(vote.genesis_hash) == 64 and vote.registry_hash

    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.AGAINST}, {q1.id: "  за  "})
    db.commit()
    cast_ballots(vote, b, {q1.id: VoteChoice.AGAINST}, source=vote_ledger.SOURCE_MANUAL, entered_by_user_id=42)
    db.commit()

    events = _events(db, vote)
    assert [e.seq for e in events] == [1, 2]
    assert events[0].prev_hash == vote.genesis_hash
    assert events[1].prev_hash == events[0].hash
    assert events[0].source == "self" and events[1].source == "manual"
    assert events[1].entered_by_user_id == 42

    result = vote_ledger.verify(vote)
    assert result.ok, result.problems
    assert result.events == 2


def test_revote_adds_event_but_keeps_single_ballot(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    db.commit()
    cast_ballots(vote, a, {q1.id: VoteChoice.AGAINST, q2.id: VoteChoice.FOR})
    db.commit()

    assert len(_events(db, vote)) == 2
    assert db.query(VoteBallot).filter_by(question_id=q1.id, person_id=a.id).count() == 1
    assert vote_ledger.verify(vote).ok


def test_close_seals_final_hash_and_verifies(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    cast_ballots(vote, b, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.AGAINST})
    db.commit()
    _close(db, vote)

    assert len(vote.final_hash) == 64
    assert vote.final_total_weight == Decimal("2")
    assert vote_ledger.verify(vote).ok


def test_closed_results_do_not_drift_when_ownership_changes(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    cast_ballots(vote, b, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    db.commit()
    _close(db, vote)
    before = question_results(q1)

    # После закрытия в кооператив добавился гараж — итоги и хеш не должны «поплыть».
    make_ownership(db, make_garage(db, number="3"), make_person(db, full_name="Новый"), share="1")
    db.commit()

    assert question_results(q1) == before
    assert vote_ledger.verify(vote).ok


def test_detects_direct_ballot_edit(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.AGAINST, q2.id: VoteChoice.AGAINST})
    db.commit()

    db.execute(text("UPDATE vote_ballot SET choice = 'FOR' WHERE question_id = :q"), {"q": q1.id})
    db.commit()
    db.expire_all()

    result = vote_ledger.verify(vote)
    assert not result.ok
    assert any("не совпадает с журналом" in p.text for p in result.problems)
    assert all(a.full_name not in p.text for p in result.problems)  # без персональных данных


def test_detects_inserted_ballot_without_event(app, db):
    vote, q1, q2, a, b = _setup(db)
    db.add(VoteBallot(question_id=q1.id, person_id=b.id, choice=VoteChoice.FOR, weight=Decimal("1"),
                      cast_at=dt.datetime.now()))
    db.commit()

    result = vote_ledger.verify(vote)
    assert any("нет в журнале" in p.text for p in result.problems)


def test_detects_question_text_change(app, db):
    vote, q1, q2, a, b = _setup(db)
    db.execute(text("UPDATE vote_question SET text = 'Утвердить смету x2?' WHERE id = :q"), {"q": q1.id})
    db.commit()
    db.expire_all()

    result = vote_ledger.verify(vote)
    assert any("genesis" in p.text for p in result.problems)


def test_detects_deleted_event_when_trigger_dropped(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    db.commit()
    cast_ballots(vote, b, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    db.commit()

    db.execute(text("DROP TRIGGER trg_vote_ballot_event_no_delete"))
    db.execute(text("DELETE FROM vote_ballot_event WHERE seq = 1 AND vote_id = :v"), {"v": vote.id})
    db.commit()
    db.expire_all()

    result = vote_ledger.verify(vote)
    assert not result.ok
    texts = " ".join(p.text for p in result.problems)
    assert "Отсутствуют записи журнала №1" in texts
    assert "нет в журнале" in texts  # бюллетень Анны остался, а его подачи в журнале нет


def test_detects_tampered_final_hash_and_post_close_edit(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    cast_ballots(vote, b, {q1.id: VoteChoice.AGAINST, q2.id: VoteChoice.AGAINST})
    db.commit()
    _close(db, vote)

    db.execute(text("UPDATE vote_ballot SET choice = 'FOR' WHERE person_id = :p"), {"p": b.id})
    db.commit()
    db.expire_all()
    texts = " ".join(p.text for p in vote_ledger.verify(vote).problems)
    assert "не совпадает с журналом" in texts and "итоговым хешем" in texts

    db.execute(text("UPDATE vote_ballot SET choice = 'AGAINST' WHERE person_id = :p"), {"p": b.id})
    db.commit()
    db.expire_all()
    assert vote_ledger.verify(vote).ok
    vote.final_hash = "0" * 64
    db.commit()
    assert any("итоговым хешем" in p.text for p in vote_ledger.verify(vote).problems)


def test_ledger_table_is_append_only(app, db):
    vote, q1, q2, a, b = _setup(db)
    cast_ballots(vote, a, {q1.id: VoteChoice.FOR, q2.id: VoteChoice.FOR})
    db.commit()

    with pytest.raises((IntegrityError, OperationalError), match="append-only"):
        db.execute(text("UPDATE vote_ballot_event SET payload = '{}'"))
    db.rollback()
    with pytest.raises((IntegrityError, OperationalError), match="append-only"):
        db.execute(text("DELETE FROM vote_ballot_event"))
    db.rollback()


def test_vote_without_ledger_is_reported_as_such(app, db):
    now = dt.datetime.now()
    vote = Vote(title="Старое", voting_type=VoteType.ABSENTEE, status=VoteStatus.CLOSED,
                opens_at=now - dt.timedelta(days=3), closes_at=now - dt.timedelta(days=1))
    db.add(vote)
    db.commit()
    result = vote_ledger.verify(vote)
    assert not result.has_ledger and not result.ok


def test_open_and_close_routes_send_hashes(app, db, client, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify_persons", lambda ids, event, subject, body, *a, **k: sent.append(body))

    chair_person = make_person(db, full_name="Председатель")
    make_ownership(db, make_garage(db, number="1"), chair_person, share="1")
    member = make_person(db, full_name="Член")
    make_ownership(db, make_garage(db, number="2"), member, share="1")
    make_user(db, "chair", "pw", role=RoleEnum.CHAIRMAN, person=chair_person)
    make_user(db, "member", "pw", person=member)
    now = dt.datetime.now()
    vote = Vote(title="Смета", voting_type=VoteType.IN_PERSON_AND_ABSENTEE, status=VoteStatus.DRAFT,
                opens_at=now - dt.timedelta(hours=1), closes_at=now + dt.timedelta(days=1))
    db.add(vote)
    db.flush()
    q = VoteQuestion(vote_id=vote.id, text="Утвердить?", majority_threshold=Decimal("0.5"))
    db.add(q)
    db.commit()
    vote_id, q_id, member_id = vote.id, q.id, member.id

    login(client, "chair", "pw")
    client.post(f"/voting/{vote_id}/open")
    db.expire_all()
    vote = db.get(Vote, vote_id)
    assert vote.genesis_hash and vote.genesis_hash in sent[-1]

    client.post(f"/voting/{vote_id}/ballot/{member_id}", data={f"choice_{q_id}": "for"})
    client.post(f"/voting/{vote_id}/ballot", data={f"choice_{q_id}": "against"})
    client.post(f"/voting/{vote_id}/close")
    db.expire_all()
    vote = db.get(Vote, vote_id)
    assert vote.final_hash and vote.final_hash in sent[-1]
    events = _events(db, vote)
    assert [e.source for e in events] == ["manual", "self"]

    resp = client.get(f"/voting/{vote_id}/integrity")
    assert "Целостность подтверждена" in resp.get_data(as_text=True)
    resp = client.get(f"/voting/{vote_id}")
    assert vote.final_hash in resp.get_data(as_text=True)

    client.get("/auth/logout")
    login(client, "member", "pw")
    resp = client.get(f"/voting/{vote_id}/integrity")
    assert resp.status_code == 200 and "Целостность подтверждена" in resp.get_data(as_text=True)
