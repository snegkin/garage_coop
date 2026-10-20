"""
Справки правления о владении гаражом (гаражная амнистия): выдаёт только
председатель, стартовый текст по данным реестра (дательный падеж, доля,
задолженность — только если её нет), журнал с номерами, печать/PDF,
видимость (правление — все, член — свои).
"""
import datetime as dt
from decimal import Decimal

from app.models import (
    AuditLog, Charge, Cooperative, FeeType, GarageCertificate, MemberAccount, RoleEnum,
)
from app.certificates import default_body

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup(db, share="1"):
    db.add(Cooperative(full_name="Гаражный кооператив «Восход»", short_name="ГСК «Восход»", inn="1234567890", kpp="123401001", ogrn="1023400000000", legal_address="г. Город, ул. Гаражная, 1"))
    owner = make_person(
        db, full_name="Петрова Анна Ивановна", birth_date=dt.date(1970, 3, 4),
        passport_series="1234", passport_number="567890", registration_address="г. Город, ул. Улица, 1",
        membership_start_date=dt.date(2001, 5, 6),
    )
    garage = make_garage(db, number="42", cadastral_number="61:44:0000000:1")
    make_ownership(db, garage, owner, share=share)
    chairman = make_person(db, full_name="Сидоров Сидор Сидорович", is_chairman=True)
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman)
    make_user(db, "owner", "pass1234", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    return owner, garage, chairman


def test_default_body_from_registry(db):
    owner, garage, _chairman = _setup(db)
    coop = db.query(Cooperative).one()
    html = default_body(owner, garage, garage.ownerships[0], coop)
    assert "Выдана Петровой Анне Ивановне" in html
    assert "она является членом ГСК «Восход» с 06.05.2001" in html.replace("&#34;", '"').replace("&laquo;", "«")
    assert "владеет гаражом № 42, расположенным на территории" in html
    assert "61:44:0000000:1" in html and "паспорт серия 1234 № 567890" in html
    assert "Задолженности по взносам и платежам перед кооперативом не имеет." in html
    assert "статьёй 3.7 Федерального закона от 25.10.2001 № 137-ФЗ" in html


def test_default_body_share_and_debt(db):
    owner, garage, _chairman = _setup(db, share="0.5")
    fee_type = FeeType(code="membership", name="Членский взнос", type_code="1")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=owner.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10420")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("300")))
    db.commit()
    html = default_body(owner, garage, garage.ownerships[0], db.query(Cooperative).one())
    assert "владеет 1/2 доли в праве на гараж № 42, расположенный" in html
    assert "Задолженности" not in html


def test_chairman_issues_certificate(db, client):
    owner, garage, chairman = _setup(db)
    login(client, "chair", "pass1234")

    page = client.get(f"/certificates/new?garage_id={garage.id}").get_data(as_text=True)
    assert "Выдана Петровой Анне Ивановне" in page and 'contenteditable="true"' in page

    resp = client.post("/certificates/new", data={
        "owner": f"{owner.id}:{garage.id}", "issued_at": "2026-10-20",
        "body_html": "<p>Выдана Петровой А.И. в том, что она владеет гаражом № 42. Паевой взнос выплачен полностью.</p><script>x</script>",
    })
    assert resp.status_code == 302
    certificate = db.query(GarageCertificate).one()
    assert certificate.number == "2026-1" and certificate.issued_by_person_id == chairman.id
    assert "<script" not in certificate.body_html and "Паевой взнос" in certificate.body_html
    assert db.query(AuditLog).filter_by(action="certificate.issue").count() == 1

    page = client.get(f"/certificates/{certificate.id}").get_data(as_text=True)
    assert "СПРАВКА" in page and "№ 2026-1" in page and "Сидоров С.С." in page
    assert client.get(f"/certificates/{certificate.id}/pdf").data[:4] == b"%PDF"
    assert f"/certificates/{certificate.id}" in client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert f"/certificates/{certificate.id}" in client.get(f"/persons/{owner.id}").get_data(as_text=True)

    # Владелец видит свою справку, но выдать сам не может.
    client.get("/auth/logout")
    login(client, "owner", "pass1234")
    assert client.get(f"/certificates/{certificate.id}").status_code == 200
    client.post("/certificates/new", data={"owner": f"{owner.id}:{garage.id}", "issued_at": "2026-10-20", "body_html": "<p>x</p>"})
    assert db.query(GarageCertificate).count() == 1


def test_board_member_cannot_issue_and_stranger_cannot_view(db, client):
    owner, garage, _chairman = _setup(db)
    board = make_person(db, full_name="Член Правления")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=board)
    stranger = make_person(db, full_name="Чужой Человек")
    make_user(db, "stranger", "pass1234", person=stranger)
    db.add(GarageCertificate(number="2026-1", person_id=owner.id, garage_id=garage.id, garage_number="42",
                             body_html="<p>Текст</p>", issued_at=dt.date(2026, 10, 20)))
    db.commit()
    certificate = db.query(GarageCertificate).one()

    login(client, "board1", "pass1234")
    assert client.get("/certificates/").status_code == 200
    client.post("/certificates/new", data={"owner": f"{owner.id}:{garage.id}", "issued_at": "2026-10-20", "body_html": "<p>x</p>"})
    assert db.query(GarageCertificate).count() == 1
    client.get("/auth/logout")

    login(client, "stranger", "pass1234")
    assert client.get(f"/certificates/{certificate.id}").status_code == 403


# ---------------------------------------------------------------------------
# Запрос справки из личного кабинета
# ---------------------------------------------------------------------------

from app import notifications
from app.models import CertificateRequest, CertificateRequestStatus


def test_member_requests_and_chairman_issues(db, client, monkeypatch):
    owner, garage, _chairman = _setup(db)
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body: sent.append((user.person_id, subject)))

    login(client, "owner", "pass1234")
    resp = client.post("/certificates/my", data={"garage_id": str(garage.id), "comment": "2 экземпляра"})
    assert resp.status_code == 302
    req = db.query(CertificateRequest).one()
    assert req.status == CertificateRequestStatus.REQUESTED and req.comment == "2 экземпляра"
    assert db.query(AuditLog).filter_by(action="certificate.request").count() == 1

    # Повторный запрос по тому же гаражу, пока первый не закрыт, — не создаётся.
    client.post("/certificates/my", data={"garage_id": str(garage.id)})
    assert db.query(CertificateRequest).count() == 1
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    assert "Запрошены справки" in client.get("/").get_data(as_text=True)
    page = client.get(f"/certificates/new?request_id={req.id}").get_data(as_text=True)
    assert "2 экземпляра" in page and "Выдана Петровой Анне Ивановне" in page
    client.post("/certificates/new", data={
        "request_id": str(req.id), "issued_at": "2026-10-20", "body_html": "<p>Справка.</p>",
    })
    db.refresh(req)
    assert req.status == CertificateRequestStatus.READY and req.certificate.person_id == owner.id
    assert (owner.id, "Справка по гаражу № 42 готова") in sent

    client.post(f"/certificates/requests/{req.id}/handed-over")
    db.refresh(req)
    assert req.status == CertificateRequestStatus.HANDED_OVER and req.handed_over_at
    client.get("/auth/logout")

    login(client, "owner", "pass1234")
    page = client.get("/certificates/my").get_data(as_text=True)
    assert "выдана на руки" in page and f"/certificates/{req.certificate_id}" in page


def test_request_only_own_garage_and_reject(db, client, monkeypatch):
    owner, garage, _chairman = _setup(db)
    other_garage = make_garage(db, number="77")
    db.commit()
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body: sent.append((user.person_id, body)))

    login(client, "owner", "pass1234")
    client.post("/certificates/my", data={"garage_id": str(other_garage.id)})
    assert db.query(CertificateRequest).count() == 0
    client.post("/certificates/my", data={"garage_id": str(garage.id)})
    req = db.query(CertificateRequest).one()
    # Сам себе не отказывает и справку по запросу не оформляет.
    client.post(f"/certificates/requests/{req.id}/reject", data={"reason": "x"})
    db.refresh(req)
    assert req.status == CertificateRequestStatus.REQUESTED
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    client.post(f"/certificates/requests/{req.id}/reject", data={"reason": "гараж в споре"})
    db.refresh(req)
    assert req.status == CertificateRequestStatus.REJECTED and req.reject_reason == "гараж в споре"
    assert any(pid == owner.id and "гараж в споре" in body for pid, body in sent)
    # Обработанный запрос не оформляется повторно.
    resp = client.get(f"/certificates/new?request_id={req.id}")
    assert resp.status_code == 302
