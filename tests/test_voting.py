"""
Тесты на вес голоса по доле владения, кворум (строго >50%) и порог принятия
решения — это правила, ошибка в которых может сделать нелегитимным
реальное решение собрания кооператива, поэтому закрепляем их тестами.
"""
import datetime as dt
from decimal import Decimal

from app.voting import (
    person_voting_weight, total_cooperative_weight, quorum_met,
    question_results, cast_ballots, eligible_voters, person_ballots_by_question,
)
from app.models import (
    Vote, VoteQuestion, VoteType, VoteStatus, VoteChoice, RoleEnum, Document, DocumentType,
)

from tests.conftest import make_garage, make_person, make_ownership, make_user, login


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


def test_ballot_comment_is_stored_and_updated_on_revote(app, db):
    """Комментарий — публичное обоснование голоса, необязательное, обновляется при переголосовании как и сам выбор."""
    garage = make_garage(db)
    voter = make_person(db)
    make_ownership(db, garage, voter, share="1")
    db.commit()

    vote, question = _make_vote(db)
    cast_ballots(vote, voter, {question.id: VoteChoice.AGAINST}, {question.id: "Слишком дорого"})
    db.commit()
    assert question.ballots[0].comment == "Слишком дорого"

    cast_ballots(vote, voter, {question.id: VoteChoice.FOR}, {question.id: "Передумал, поддерживаю"})
    db.commit()
    assert len(question.ballots) == 1
    assert question.ballots[0].comment == "Передумал, поддерживаю"

    # без комментария — поле пустое, не ошибка
    cast_ballots(vote, voter, {question.id: VoteChoice.FOR})
    db.commit()
    assert question.ballots[0].comment is None


# ---------------------------------------------------------------------------
# eligible_voters()
# ---------------------------------------------------------------------------

def test_eligible_voters_excludes_people_without_ownership(app, db):
    garage = make_garage(db)
    owner = make_person(db, full_name="Owner")
    bystander = make_person(db, full_name="Bystander")  # ни одной доли
    make_ownership(db, garage, owner, share="1")
    db.commit()

    ids = {p.id for p in eligible_voters()}
    assert owner.id in ids
    assert bystander.id not in ids


def test_eligible_voters_sorted_by_name(app, db):
    garage1 = make_garage(db, number="1")
    garage2 = make_garage(db, number="2")
    z_person = make_person(db, full_name="Яковлев")
    a_person = make_person(db, full_name="Абрамов")
    make_ownership(db, garage1, z_person, share="1")
    make_ownership(db, garage2, a_person, share="1")
    db.commit()

    voters = eligible_voters()
    names = [p.full_name for p in voters]
    assert names == sorted(names)


def test_ballot_comment_is_publicly_visible_to_other_members_while_vote_open(app, db, client):
    """
    Комментарий — публичный: виден ЛЮБОМУ члену кооператива сразу, не
    дожидаясь закрытия голосования (в отличие от агрегированных итогов,
    см. voting.question_results, которые до закрытия видит только
    правление) — человек вправе аргументировать позицию для остальных.
    """
    garage = make_garage(db)
    voter = make_person(db, full_name="Голосующий")
    make_ownership(db, garage, voter, share="1")
    make_user(db, "voter", "pass1234", role=RoleEnum.MEMBER, person=voter)

    other = make_person(db, full_name="Другой Член")
    make_ownership(db, make_garage(db, number="2"), other, share="1")
    make_user(db, "other", "pass1234", role=RoleEnum.MEMBER, person=other)
    db.commit()

    vote, question = _make_vote(db)
    db.commit()

    login(client, "voter", "pass1234")
    client.post(f"/voting/{vote.id}/ballot", data={f"choice_{question.id}": "against", f"comment_{question.id}": "Слишком дорого для бюджета"})
    client.get("/auth/logout")

    login(client, "other", "pass1234")
    resp = client.get(f"/voting/{vote.id}")
    html = resp.get_data(as_text=True)
    assert "Слишком дорого для бюджета" in html
    assert "Голосующий" in html


# ---------------------------------------------------------------------------
# Ручная запись очных голосов председателем (очно-заочное голосование)
# ---------------------------------------------------------------------------

def _make_hybrid_vote(db, status=VoteStatus.OPEN):
    """Голосование типа IN_PERSON_AND_ABSENTEE — единственный тип, для
    которого допустима ручная запись председателем (см. set_ballot_for_person)."""
    now = dt.datetime.now()
    vote = Vote(
        title="Очно-заочное голосование",
        voting_type=VoteType.IN_PERSON_AND_ABSENTEE,
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


def _make_chairman(db, username="chair1"):
    person = make_person(db, full_name="Chairman One")
    make_user(db, username, "pass1234", role=RoleEnum.CHAIRMAN, person=person)
    db.commit()
    return person


def test_chairman_can_record_ballot_for_hybrid_vote(app, db, client):
    garage = make_garage(db)
    voter = make_person(db, full_name="Voter")
    make_ownership(db, garage, voter, share="1")
    _make_chairman(db)
    vote, question = _make_hybrid_vote(db)
    db.commit()

    login(client, "chair1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot/{voter.id}", data={
        f"choice_{question.id}": "for",
    })
    assert resp.status_code == 302

    ballots = person_ballots_by_question(vote, voter.id)
    assert question.id in ballots
    assert ballots[question.id].choice == VoteChoice.FOR


def test_manual_ballot_rejected_for_absentee_vote_type(app, db, client):
    """Ручная запись доступна только для очно-заочного — для чисто
    заочного голосования весь процесс электронный, вмешательство
    председателя не предусмотрено."""
    garage = make_garage(db)
    voter = make_person(db, full_name="Voter")
    make_ownership(db, garage, voter, share="1")
    _make_chairman(db)
    vote, question = _make_vote(db)  # ABSENTEE
    db.commit()

    login(client, "chair1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot/{voter.id}", data={
        f"choice_{question.id}": "for",
    }, follow_redirects=True)
    assert resp.status_code == 200

    ballots = person_ballots_by_question(vote, voter.id)
    assert question.id not in ballots


def test_manual_ballot_rejected_when_vote_not_open(app, db, client):
    garage = make_garage(db)
    voter = make_person(db, full_name="Voter")
    make_ownership(db, garage, voter, share="1")
    _make_chairman(db)
    vote, question = _make_hybrid_vote(db, status=VoteStatus.DRAFT)
    db.commit()

    login(client, "chair1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot/{voter.id}", data={
        f"choice_{question.id}": "for",
    })
    assert resp.status_code == 302

    ballots = person_ballots_by_question(vote, voter.id)
    assert question.id not in ballots


def test_manual_ballot_rejected_for_person_without_share(app, db, client):
    bystander = make_person(db, full_name="Bystander")
    _make_chairman(db)
    vote, question = _make_hybrid_vote(db)
    db.commit()

    login(client, "chair1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot/{bystander.id}", data={
        f"choice_{question.id}": "for",
    })
    assert resp.status_code == 302

    ballots = person_ballots_by_question(vote, bystander.id)
    assert question.id not in ballots


def test_manual_ballot_forbidden_for_non_chairman(app, db, client):
    """Право есть только у председателя — не у любого члена правления."""
    garage = make_garage(db)
    voter = make_person(db, full_name="Voter")
    make_ownership(db, garage, voter, share="1")

    board_person = make_person(db, full_name="Board One")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=board_person)
    db.commit()

    vote, question = _make_hybrid_vote(db)
    db.commit()

    login(client, "board1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot/{voter.id}", data={
        f"choice_{question.id}": "for",
    })
    assert resp.status_code == 302

    ballots = person_ballots_by_question(vote, voter.id)
    assert question.id not in ballots


def test_chairman_can_record_partial_ballot(app, db, client):
    """Председатель может отметить не все вопросы сразу — незаполненные
    остаются нетронутыми (см. docstring set_ballot_for_person)."""
    garage = make_garage(db)
    voter = make_person(db, full_name="Voter")
    make_ownership(db, garage, voter, share="1")
    _make_chairman(db)
    vote, question1 = _make_hybrid_vote(db)
    question2 = VoteQuestion(vote_id=vote.id, text="Второй вопрос", majority_threshold=Decimal("0.5"))
    db.add(question2)
    db.commit()

    login(client, "chair1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot/{voter.id}", data={
        f"choice_{question1.id}": "for",
    })
    assert resp.status_code == 302

    ballots = person_ballots_by_question(vote, voter.id)
    assert question1.id in ballots
    assert question2.id not in ballots


def test_member_can_still_revote_after_chairman_recorded_it(app, db, client):
    """Последняя подача побеждает — включая случай, когда сначала
    председатель записал голос вручную, а потом человек сам переголосовал
    электронно."""
    garage = make_garage(db)
    voter_person = make_person(db, full_name="Voter")
    make_ownership(db, garage, voter_person, share="1")
    make_user(db, "voter1", "pass1234", role=RoleEnum.MEMBER, person=voter_person)
    _make_chairman(db)
    vote, question = _make_hybrid_vote(db)
    db.commit()

    login(client, "chair1", "pass1234")
    client.post(f"/voting/{vote.id}/ballot/{voter_person.id}", data={f"choice_{question.id}": "against"})
    client.get("/auth/logout")

    login(client, "voter1", "pass1234")
    resp = client.post(f"/voting/{vote.id}/ballot", data={f"choice_{question.id}": "for"})
    assert resp.status_code == 302

    ballots = person_ballots_by_question(vote, voter_person.id)
    assert ballots[question.id].choice == VoteChoice.FOR
    assert len(question.ballots) == 1  # тот же бюллетень обновлён, не задвоен


# ---------------------------------------------------------------------------
# Полностью очное голосование (VoteType.IN_PERSON)
# ---------------------------------------------------------------------------

def test_create_in_person_vote_requires_protocol_file(app, db, client):
    _make_chairman(db)
    login(client, "chair1", "pass1234")

    resp = client.post("/voting/new", data={
        "title": "Собрание 12.01.2026",
        "voting_type": "in_person",
        "vote_date": "2026-01-12T18:00",
    })
    assert resp.status_code == 302

    votes = db.query(Vote).filter_by(title="Собрание 12.01.2026").all()
    assert votes == []


def test_create_in_person_vote_requires_date(app, db, client):
    from io import BytesIO
    _make_chairman(db)
    login(client, "chair1", "pass1234")

    resp = client.post("/voting/new", data={
        "title": "Собрание без даты",
        "voting_type": "in_person",
        "protocol_file": (BytesIO(b"%PDF-fake"), "protocol.pdf"),
    })
    assert resp.status_code == 302

    votes = db.query(Vote).filter_by(title="Собрание без даты").all()
    assert votes == []


def test_create_in_person_vote_success(app, db, client):
    from io import BytesIO
    _make_chairman(db)
    login(client, "chair1", "pass1234")

    resp = client.post("/voting/new", data={
        "title": "Собрание 12.01.2026",
        "voting_type": "in_person",
        "vote_date": "2026-01-12T18:00",
        "protocol_file": (BytesIO(b"%PDF-fake"), "protocol.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    vote = db.query(Vote).filter_by(title="Собрание 12.01.2026").first()
    assert vote is not None
    assert vote.voting_type == VoteType.IN_PERSON
    assert vote.status == VoteStatus.CLOSED
    assert vote.opens_at == vote.closes_at
    assert vote.protocol_document_id is not None
    assert vote.questions == []

    doc = db.query(Document).get(vote.protocol_document_id)
    assert doc is not None
    assert doc.doc_type == DocumentType.PROTOCOL
    assert doc.file_path is not None


def test_in_person_vote_type_is_valid_enum_value():
    assert VoteType("in_person") == VoteType.IN_PERSON
