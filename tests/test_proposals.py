"""
Тесты на предложения голосований от членов кооператива: подача, голос
правления (по головам, а не по долям), автоматическое подведение итога
(все проголосовали / истёк недельный срок), создание Vote-черновика при
одобрении.
"""
import datetime as dt

from app.models import (
    RoleEnum, VoteProposal, VoteProposalBoardBallot, ProposalStatus, VoteChoice,
    BoardTerm, BoardMember, Vote, VoteStatus, PROPOSAL_REVIEW_PERIOD,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _make_board(db, size=2):
    """Текущий (открытый) созыв правления из `size` человек — первый председатель. Возвращает список (person, user, username, password)."""
    term = BoardTerm(start_date=dt.date(2024, 1, 1))
    db.add(term)
    db.flush()

    members = []
    for i in range(size):
        person = make_person(db, full_name=f"Правленец {i}")
        username = f"board{i}"
        make_user(db, username, "pass1234", role=RoleEnum.CHAIRMAN if i == 0 else RoleEnum.BOARD, person=person)
        db.add(BoardMember(term_id=term.id, person_id=person.id, is_chairman=(i == 0)))
        members.append((person, username))
    db.commit()
    return members


def _make_owner_member(db, username="owner"):
    person = make_person(db, full_name="Собственник Гаражный")
    garage = make_garage(db, number="42")
    make_ownership(db, garage, person)
    make_user(db, username, "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()
    return person


def test_owner_can_propose_vote(app, db, client):
    _make_owner_member(db)
    login(client, "owner", "pass1234")

    resp = client.post("/proposals/new", data={"title": "Установить шлагбаум", "description": "Обсуждали на форуме"})
    assert resp.status_code == 302

    proposal = db.query(VoteProposal).filter_by(title="Установить шлагбаум").first()
    assert proposal is not None
    assert proposal.status == ProposalStatus.PENDING


def test_non_owner_cannot_propose(app, db, client):
    person = make_person(db, full_name="Без гаража")
    make_user(db, "noowner", "pass1234", role=RoleEnum.MEMBER, person=person)
    db.commit()

    login(client, "noowner", "pass1234")
    client.post("/proposals/new", data={"title": "Что-нибудь"})

    assert db.query(VoteProposal).count() == 0


def test_all_board_approve_creates_draft_vote(app, db, client):
    owner = _make_owner_member(db)
    board = _make_board(db, size=2)

    proposal = VoteProposal(title="Новый шлагбаум", proposed_by_person_id=owner.id, created_at=dt.datetime.now())
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    for _, username in board:
        login(client, username, "pass1234")
        resp = client.post(f"/proposals/{proposal_id}/board-vote", data={"choice": "for"})
        assert resp.status_code == 302
        client.get("/auth/logout")

    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    assert proposal.status == ProposalStatus.APPROVED
    assert proposal.resulting_vote_id is not None
    vote = db.get(Vote, proposal.resulting_vote_id)
    assert vote.title == "Новый шлагбаум"
    assert vote.status == VoteStatus.DRAFT


def test_majority_against_rejects_without_creating_vote(app, db, client):
    owner = _make_owner_member(db)
    board = _make_board(db, size=2)

    proposal = VoteProposal(
        title="Спорная идея", proposed_by_person_id=owner.id, created_at=dt.datetime.now(),
    )
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    choices = ["for", "against"]
    for (_, username), choice in zip(board, choices):
        login(client, username, "pass1234")
        client.post(f"/proposals/{proposal_id}/board-vote", data={"choice": choice})
        client.get("/auth/logout")

    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    # 1 за, 1 против — ничья, решение не принято (правление не набрало
    # большинства "за"), но обе резолюции ещё не наступили (не все
    # проголосовали за/против однозначно — тут как раз все проголосовали)
    assert proposal.status == ProposalStatus.REJECTED
    assert proposal.resulting_vote_id is None


def test_deadline_resolves_pending_proposal_without_all_votes(app, db, client):
    owner = _make_owner_member(db)
    board = _make_board(db, size=3)

    proposal = VoteProposal(
        title="Долгожданное предложение", proposed_by_person_id=owner.id,
        created_at=dt.datetime.now() - PROPOSAL_REVIEW_PERIOD - dt.timedelta(hours=1),
    )
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    # Только один из трёх членов правления успел проголосовать "за" — но
    # срок уже истёк, так что решение должно подвестись при первом же
    # обращении к списку/карточке.
    chair_person, chair_username = board[0]
    db.add(VoteProposalBoardBallot(proposal_id=proposal_id, person_id=chair_person.id, choice=VoteChoice.FOR, voted_at=dt.datetime.now()))
    db.commit()

    login(client, chair_username, "pass1234")
    resp = client.get(f"/proposals/{proposal_id}")
    assert resp.status_code == 200

    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    assert proposal.status == ProposalStatus.APPROVED
    assert proposal.resulting_vote_id is not None


def test_chairman_can_edit_pending_proposal_but_not_after_resolution(app, db, client):
    owner = _make_owner_member(db)
    board = _make_board(db, size=1)
    chair_person, chair_username = board[0]

    proposal = VoteProposal(title="Черновая формулировка", proposed_by_person_id=owner.id, created_at=dt.datetime.now())
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    login(client, chair_username, "pass1234")
    resp = client.post(f"/proposals/{proposal_id}/edit", data={"title": "Уточнённая формулировка", "description": ""})
    assert resp.status_code == 302

    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    assert proposal.title == "Уточнённая формулировка"

    # единственный член правления голосует "за" -> решение принимается сразу
    resp = client.post(f"/proposals/{proposal_id}/board-vote", data={"choice": "for"})
    assert resp.status_code == 302

    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    assert proposal.status == ProposalStatus.APPROVED

    resp = client.post(f"/proposals/{proposal_id}/edit", data={"title": "Попытка правки после решения"})
    assert resp.status_code == 302
    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    assert proposal.title == "Уточнённая формулировка"


def test_member_cannot_cast_board_vote(app, db, client):
    owner = _make_owner_member(db)
    _make_board(db, size=1)

    proposal = VoteProposal(title="Тема", proposed_by_person_id=owner.id, created_at=dt.datetime.now())
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    login(client, "owner", "pass1234")
    client.post(f"/proposals/{proposal_id}/board-vote", data={"choice": "for"})

    assert db.query(VoteProposalBoardBallot).count() == 0


def test_board_vote_comment_is_stored_and_publicly_visible(app, db, client):
    """Комментарий члена правления к голосу за/против — публичный, виден
    рядовому члену кооператива на карточке предложения (не только правлению)."""
    owner = _make_owner_member(db)
    board = _make_board(db, size=2)
    chair_person, chair_username = board[0]

    proposal = VoteProposal(title="Спорный вопрос", proposed_by_person_id=owner.id, created_at=dt.datetime.now())
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    login(client, chair_username, "pass1234")
    resp = client.post(f"/proposals/{proposal_id}/board-vote", data={"choice": "against", "comment": "Не согласован бюджет"})
    assert resp.status_code == 302
    client.get("/auth/logout")

    db.expire_all()
    ballot = db.query(VoteProposalBoardBallot).filter_by(proposal_id=proposal_id, person_id=chair_person.id).first()
    assert ballot.comment == "Не согласован бюджет"

    login(client, "owner", "pass1234")
    resp = client.get(f"/proposals/{proposal_id}")
    html = resp.get_data(as_text=True)
    assert "Не согласован бюджет" in html
    assert chair_person.full_name in html


def test_board_can_abstain_and_it_does_not_tip_the_decision(app, db, client):
    """«Воздержался» — валидный выбор правления (не только за/против), но не засчитывается ни в чью пользу."""
    owner = _make_owner_member(db)
    board = _make_board(db, size=2)

    proposal = VoteProposal(title="Спорная тема", proposed_by_person_id=owner.id, created_at=dt.datetime.now())
    db.add(proposal)
    db.commit()
    proposal_id = proposal.id

    for _, username in board:
        login(client, username, "pass1234")
        resp = client.post(f"/proposals/{proposal_id}/board-vote", data={"choice": "abstain"})
        assert resp.status_code == 302
        client.get("/auth/logout")

    db.expire_all()
    proposal = db.get(VoteProposal, proposal_id)
    # оба воздержались -> "за" не больше "против" (0:0) -> отклонено, Vote не создан
    assert proposal.status == ProposalStatus.REJECTED
    assert proposal.resulting_vote_id is None

    ballots = db.query(VoteProposalBoardBallot).filter_by(proposal_id=proposal_id).all()
    assert all(b.choice == VoteChoice.ABSTAIN for b in ballots)
