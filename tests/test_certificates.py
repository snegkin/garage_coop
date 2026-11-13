"""
Справки о владении гаражом (гаражная амнистия) — шаблон раздела «Бланки и
шаблоны» (code="garage_certificate"): составляет только председатель,
текст по реестру (дательный падеж, доля, «задолженности не имеет» —
только если долга нет), номер в общем журнале документов, печать/PDF,
видимость (правление — все, член — свои), запросы из личного кабинета.
"""
import datetime as dt
from decimal import Decimal

from app import documents
from app.models import (
    AuditLog, Charge, Cooperative, FeeType, IssuedDocument, MemberAccount, RoleEnum,
)

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


def _body(app, db, owner, garage):
    with app.test_request_context():
        template = documents.certificate_template()
        ctx = documents.Context(dt.date(2026, 10, 20), owner, garage)
        return documents.fill(template.body_html, ctx).replace("&laquo;", "«").replace("&raquo;", "»")


def test_default_body_from_registry(db, app):
    owner, garage, _chairman = _setup(db)
    html = _body(app, db, owner, garage)
    assert "Выдана Петровой Анне Ивановне (04.03.1970 г. р.; паспорт серия 1234 № 567890" in html
    assert "в том, что она является членом ГСК «Восход» с 06.05.2001 и владеет гаражом № 42, расположенным на территории" in html
    assert "61:44:0000000:1" in html and "площадью 18,00 м²" in html
    assert "<p>Задолженности по взносам и платежам перед кооперативом не имеет.</p>" in html
    assert "Справка выдана для предъявления по месту требования." in html
    assert "137-ФЗ" not in html


def test_default_body_share_and_debt(db, app):
    owner, garage, _chairman = _setup(db, share="0.5")
    owner.membership_end_date = dt.date(2020, 1, 1)
    fee_type = FeeType(code="membership", name="Членский взнос", type_code="1")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=owner.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10420")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("300")))
    db.commit()
    html = _body(app, db, owner, garage)
    assert "она владеет 1/2 доли в праве на гараж № 42, расположенный" in html
    # Выбывший — без «является членом», при долге — без фразы и без пустого абзаца.
    assert "является членом" not in html and "Задолженности" not in html and "<p></p>" not in html


def test_chairman_issues_certificate(db, client):
    owner, garage, chairman = _setup(db)
    login(client, "chair", "pass1234")

    # Кнопка на карточке гаража ведёт в составление документа по шаблону справки.
    resp = client.get(f"/certificates/new?garage_id={garage.id}")
    assert resp.status_code == 302 and "/documents/new?template_id=" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Выдана Петровой Анне Ивановне" in page and 'id="docBody"' in page
    template = documents_template = db.query(documents.DocumentTemplate).filter_by(code="garage_certificate").one()
    assert documents_template.chairman_only

    resp = client.post("/documents/new", data={
        "template_id": template.id, "owner": f"{owner.id}:{garage.id}", "issued_at": "2026-10-20",
        "title": "СПРАВКА", "addressee_html": "<p>по месту требования</p>",
        "body_html": "<p>Выдана Петровой А.И. в том, что она владеет гаражом № 42. Паевой взнос выплачен полностью.</p><script>x</script>",
    })
    assert resp.status_code == 302
    certificate = db.query(IssuedDocument).one()
    assert certificate.number == "2026-1" and certificate.signer_person_id == chairman.id
    assert "<script" not in certificate.body_html and "Паевой взнос" in certificate.body_html
    assert db.query(AuditLog).filter_by(action="issued_document.create").count() == 1

    page = client.get(f"/documents/{certificate.id}").get_data(as_text=True)
    assert "СПРАВКА" in page and "№ 2026-1" in page and "Сидоров С.С." in page and "по месту требования" in page
    assert client.get(f"/documents/{certificate.id}/pdf").data[:4] == b"%PDF"
    assert f"/documents/{certificate.id}" in client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert f"/documents/{certificate.id}" in client.get(f"/persons/{owner.id}").get_data(as_text=True)

    # Владелец видит свою справку (и в кабинете), но составить сам не может.
    client.get("/auth/logout")
    login(client, "owner", "pass1234")
    assert client.get(f"/documents/{certificate.id}").status_code == 200
    assert f"/documents/{certificate.id}" in client.get("/certificates/my").get_data(as_text=True)
    client.post("/documents/new", data={"template_id": template.id, "owner": f"{owner.id}:{garage.id}",
                                        "issued_at": "2026-10-20", "body_html": "<p>x</p>"})
    assert db.query(IssuedDocument).count() == 1


def test_board_member_cannot_issue_and_stranger_cannot_view(db, client, app):
    owner, garage, _chairman = _setup(db)
    board = make_person(db, full_name="Член Правления")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=board)
    stranger = make_person(db, full_name="Чужой Человек")
    make_user(db, "stranger", "pass1234", person=stranger)
    with app.test_request_context():
        template = documents.certificate_template()
    db.add(IssuedDocument(template_id=template.id, number="2026-1", title="СПРАВКА", addressee_html="",
                          person_id=owner.id, garage_id=garage.id, body_html="<p>Текст</p>",
                          issued_at=dt.date(2026, 10, 20)))
    db.commit()
    certificate = db.query(IssuedDocument).one()

    login(client, "board1", "pass1234")
    # Отдельного раздела у справок нет — журнал в «Бланках и шаблонах».
    resp = client.get("/certificates/")
    assert resp.status_code == 302 and "/documents/" in resp.headers["Location"]
    page = client.get("/documents/").get_data(as_text=True)
    assert f"/documents/{certificate.id}" in page and "2026-1" in page
    client.post("/documents/new", data={"template_id": template.id, "owner": f"{owner.id}:{garage.id}",
                                        "issued_at": "2026-10-20", "body_html": "<p>x</p>"})
    assert db.query(IssuedDocument).count() == 1
    client.post(f"/documents/{certificate.id}/delete")
    assert db.query(IssuedDocument).count() == 1
    client.get("/auth/logout")

    login(client, "stranger", "pass1234")
    assert client.get(f"/documents/{certificate.id}").status_code == 403
    assert client.get(f"/documents/{certificate.id}/pdf").status_code == 403


# ---------------------------------------------------------------------------
# Запрос справки из личного кабинета
# ---------------------------------------------------------------------------

from app import notifications
from app.models import CertificateRequest, CertificateRequestStatus


def test_member_requests_and_chairman_issues(db, client, monkeypatch):
    owner, garage, _chairman = _setup(db)
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body, **kw: sent.append((user.person_id, subject)))

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
    page = client.get(f"/documents/new?request_id={req.id}").get_data(as_text=True)
    assert "2 экземпляра" in page and "Выдана Петровой Анне Ивановне" in page
    client.post("/documents/new", data={
        "request_id": str(req.id), "issued_at": "2026-10-20", "title": "СПРАВКА", "body_html": "<p>Справка.</p>",
    })
    db.refresh(req)
    assert req.status == CertificateRequestStatus.READY and req.document.person_id == owner.id
    assert req.document.garage_id == garage.id and req.document.number == "2026-1"
    assert (owner.id, "Справка по гаражу № 42 готова") in sent

    client.post(f"/certificates/requests/{req.id}/handed-over")
    db.refresh(req)
    assert req.status == CertificateRequestStatus.HANDED_OVER and req.handed_over_at
    client.get("/auth/logout")

    login(client, "owner", "pass1234")
    page = client.get("/certificates/my").get_data(as_text=True)
    assert "выдана на руки" in page and f"/documents/{req.document_id}" in page


def test_request_only_own_garage_and_reject(db, client, monkeypatch):
    owner, garage, _chairman = _setup(db)
    other_garage = make_garage(db, number="77")
    db.commit()
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body, **kw: sent.append((user.person_id, body)))

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
    resp = client.get(f"/documents/new?request_id={req.id}")
    assert resp.status_code == 302
