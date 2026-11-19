"""
Самовыдвижение в правление/ревизионную комиссию/председатели из личного
кабинета (app/elections.py, cabinet.candidacy*): право — только у
собственника гаража на территории кооператива.
"""
from app.models import AuditLog, ElectionCandidacy, ElectionPosition, RoleEnum

from tests.conftest import login, make_garage, make_ownership, make_person, make_user


def _member(db, username, *, outside=False, full_name="Кандидатов Кандидат Кандидатович", number="501"):
    person = make_person(db, full_name=full_name)
    garage = make_garage(db, number=number, outside_territory=outside)
    make_ownership(db, garage, person)
    make_user(db, username, "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    return person, garage


def test_owner_on_territory_can_nominate_and_withdraw(db, client):
    person, _ = _member(db, "cand1")
    login(client, "cand1", "pass12345")

    page = client.get("/cabinet/candidacy").get_data(as_text=True)
    assert "Выдвинуть кандидатуру" in page

    resp = client.post("/cabinet/candidacy", data={"position": "chairman", "statement": "Наведу порядок"})
    assert resp.status_code == 302
    c = db.query(ElectionCandidacy).filter_by(person_id=person.id).one()
    assert c.position == ElectionPosition.CHAIRMAN and c.statement == "Наведу порядок"
    assert db.query(AuditLog).filter_by(action="candidacy.nominate").count() == 1

    # Повторная подача — правка текста, не дубль.
    client.post("/cabinet/candidacy", data={"position": "chairman", "statement": "Новый текст"})
    db.expire_all()
    assert db.query(ElectionCandidacy).filter_by(person_id=person.id).one().statement == "Новый текст"

    governance = client.get("/governance/").get_data(as_text=True)
    assert "Кандидатов Кандидат Кандидатович" in governance and "Новый текст" in governance

    client.post("/cabinet/candidacy/withdraw", data={"position": "chairman"})
    assert db.query(ElectionCandidacy).filter_by(person_id=person.id).count() == 0
    assert db.query(AuditLog).filter_by(action="candidacy.withdraw").count() == 1


def test_owner_outside_territory_cannot_nominate(db, client):
    person, _ = _member(db, "cand2", outside=True)
    login(client, "cand2", "pass12345")

    page = client.get("/cabinet/candidacy").get_data(as_text=True)
    assert "Выдвинуть кандидатуру" not in page

    client.post("/cabinet/candidacy", data={"position": "board"})
    assert db.query(ElectionCandidacy).filter_by(person_id=person.id).count() == 0


def test_person_without_garage_cannot_nominate(db, client):
    person = make_person(db, full_name="Безгаражный Человек")
    make_user(db, "cand3", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "cand3", "pass12345")

    client.post("/cabinet/candidacy", data={"position": "revision_commission"})
    assert db.query(ElectionCandidacy).filter_by(person_id=person.id).count() == 0


def test_candidacy_hidden_after_losing_garage(db, client):
    """Продал гараж — заявка не выводится в списке кандидатов."""
    person, garage = _member(db, "cand4", full_name="Бывший Владелец Гаража")
    db.add(ElectionCandidacy(person_id=person.id, position=ElectionPosition.BOARD))
    db.commit()
    login(client, "cand4", "pass12345")
    assert "Бывший Владелец Гаража" in client.get("/governance/").get_data(as_text=True)

    garage.outside_territory = True
    db.commit()
    assert "Бывший Владелец Гаража" not in client.get("/governance/").get_data(as_text=True)


def test_unknown_position_rejected(db, client):
    person, _ = _member(db, "cand5")
    login(client, "cand5", "pass12345")
    client.post("/cabinet/candidacy", data={"position": "president"})
    assert db.query(ElectionCandidacy).filter_by(person_id=person.id).count() == 0


def test_candidates_marked_in_term_and_commission_member_lists(db, client):
    import datetime as dt
    from app.models import BoardTerm, RevisionCommission

    person, _ = _member(db, "cand6", full_name="Отмеченный Кандидат")
    db.add_all([
        ElectionCandidacy(person_id=person.id, position=ElectionPosition.CHAIRMAN),
        ElectionCandidacy(person_id=person.id, position=ElectionPosition.REVISION_COMMISSION),
    ])
    term = BoardTerm(start_date=dt.date(2026, 1, 1))
    commission = RevisionCommission(start_date=dt.date(2026, 1, 1))
    db.add_all([term, commission])
    make_user(db, "chair6", "pass12345", role=RoleEnum.CHAIRMAN, person=make_person(db, full_name="Председатель"))
    db.commit()
    login(client, "chair6", "pass12345")

    assert "Отмеченный Кандидат — кандидат в председатели" in client.get(f"/governance/board-terms/{term.id}").get_data(as_text=True)
    assert "Отмеченный Кандидат — кандидат в комиссию" in client.get(f"/governance/revision-commissions/{commission.id}").get_data(as_text=True)


def test_cannot_run_for_commission_and_board_at_once(db, client):
    """Устав: ревизор не может быть членом правления/председателем."""
    person, _ = _member(db, "cand7")
    login(client, "cand7", "pass12345")

    client.post("/cabinet/candidacy", data={"position": "board"})
    client.post("/cabinet/candidacy", data={"position": "revision_commission"})
    positions = {c.position for c in db.query(ElectionCandidacy).filter_by(person_id=person.id)}
    assert positions == {ElectionPosition.BOARD}

    # В председатели и в правление одновременно — можно.
    client.post("/cabinet/candidacy", data={"position": "chairman"})
    positions = {c.position for c in db.query(ElectionCandidacy).filter_by(person_id=person.id)}
    assert positions == {ElectionPosition.BOARD, ElectionPosition.CHAIRMAN}

    page = client.get("/cabinet/candidacy").get_data(as_text=True)
    assert "Недоступно, пока вы кандидат в другой орган" in page

    client.post("/cabinet/candidacy/withdraw", data={"position": "board"})
    client.post("/cabinet/candidacy/withdraw", data={"position": "chairman"})
    client.post("/cabinet/candidacy", data={"position": "revision_commission"})
    client.post("/cabinet/candidacy", data={"position": "chairman"})
    positions = {c.position for c in db.query(ElectionCandidacy).filter_by(person_id=person.id)}
    assert positions == {ElectionPosition.REVISION_COMMISSION}
