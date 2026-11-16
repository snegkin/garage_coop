"""
Справки о владении гаражом (гаражная амнистия) — шаблон раздела «Бланки и
шаблоны» (code="garage_certificate"): составляет только председатель,
текст по реестру (дательный падеж, доля, «задолженности не имеет» —
только если долга нет), номер в общем журнале документов, печать/PDF,
видимость (правление — все, член — свои), заявление о выдаче справки из
личного кабинета.
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
# Заявление о выдаче справки из личного кабинета
# ---------------------------------------------------------------------------

from app import notifications
from app.models import Application, ApplicationKind, ApplicationStatus


def _submit_certificate_application(client, garage):
    return client.post("/applications/new", data={
        "kind": "certificate", "garage_id": str(garage.id), "title": "о выдаче справки о владении гаражом",
        "body_html": "<p>Прошу выдать справку о владении гаражом № 42. Нужно 2 экземпляра.</p>",
    })


def test_member_applies_and_chairman_issues(db, client, monkeypatch):
    owner, garage, _chairman = _setup(db)
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body, **kw: sent.append((user.person_id, subject)))

    login(client, "owner", "pass1234")
    # В кабинете одна кнопка «Заявление в правление», справка — вид заявления.
    page = client.get("/cabinet/garages").get_data(as_text=True)
    assert f"/applications/new?garage_id={garage.id}&amp;kind=certificate" in page
    assert "/certificates/my" not in page and "/certificates/my" not in client.get("/").get_data(as_text=True)
    # Старая ссылка ведёт в редактор заявления этого вида.
    resp = client.get(f"/certificates/my?garage_id={garage.id}")
    assert resp.status_code == 302 and "kind=certificate" in resp.headers["Location"]
    editor = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Справка о владении гаражом" in editor and "являюсь членом кооператива" in editor

    assert _submit_certificate_application(client, garage).status_code == 302
    application = db.query(Application).one()
    assert application.kind == ApplicationKind.CERTIFICATE and application.garage_id == garage.id
    # Заявление подшито к гаражу.
    assert f"/applications/{application.id}" in client.get(f"/garages/{garage.id}").get_data(as_text=True)
    assert "/documents/new?application_id=" not in client.get(f"/applications/{application.id}").get_data(as_text=True)
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    assert "Заявления ждут решения" in client.get("/").get_data(as_text=True)
    page = client.get(f"/applications/{application.id}").get_data(as_text=True)
    assert f"/documents/new?application_id={application.id}" in page
    page = client.get(f"/documents/new?application_id={application.id}").get_data(as_text=True)
    assert f"№ {application.number}" in page and "Выдана Петровой Анне Ивановне" in page
    client.post("/documents/new", data={
        "application_id": str(application.id), "issued_at": "2026-10-20", "title": "СПРАВКА", "body_html": "<p>Справка.</p>",
    })
    db.refresh(application)
    certificate = db.query(IssuedDocument).one()
    assert application.status == ApplicationStatus.ANSWERED and application.document_id == certificate.id
    assert certificate.person_id == owner.id and certificate.garage_id == garage.id and certificate.number == "2026-1"
    assert "заберите подписанный экземпляр у председателя" in application.response_html
    assert (owner.id, "Справка по гаражу № 42 готова") in sent
    # Повторно по закрытому заявлению справка не оформляется.
    assert client.get(f"/documents/new?application_id={application.id}").status_code == 302

    # Удалили справку из журнала — заявление снова ждёт решения.
    client.post(f"/documents/{certificate.id}/delete")
    db.refresh(application)
    assert application.status == ApplicationStatus.SUBMITTED and application.document_id is None
    assert application.response_html is None
    client.get("/auth/logout")

    login(client, "owner", "pass1234")
    page = client.get(f"/applications/{application.id}").get_data(as_text=True)
    assert "оформляет председатель" in page


def test_certificate_only_by_chairman_and_reject(db, client, monkeypatch):
    owner, garage, _chairman = _setup(db)
    board = make_person(db, full_name="Член Правления")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=board)
    db.commit()
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda user, event, subject, body, **kw: sent.append((user.person_id, body)))

    login(client, "owner", "pass1234")
    _submit_certificate_application(client, garage)
    application = db.query(Application).one()
    # Сам заявитель справку по своему заявлению не оформляет.
    client.post("/documents/new", data={"application_id": str(application.id), "issued_at": "2026-10-20",
                                        "body_html": "<p>x</p>"})
    assert db.query(IssuedDocument).count() == 0
    client.get("/auth/logout")

    # Член правления видит заявление, но справку оформляет только председатель.
    login(client, "board1", "pass1234")
    page = client.get(f"/applications/{application.id}").get_data(as_text=True)
    assert "/documents/new?application_id=" not in page and "оформляет председатель" in page
    client.post("/documents/new", data={"application_id": str(application.id), "issued_at": "2026-10-20",
                                        "body_html": "<p>x</p>"})
    assert db.query(IssuedDocument).count() == 0
    # Отклонить может любой член правления — как обычное заявление.
    client.post(f"/applications/{application.id}/decide", data={"decision": "reject", "response_html": "<p>гараж в споре</p>"})
    db.refresh(application)
    assert application.status == ApplicationStatus.REJECTED and "гараж в споре" in application.response_html
    assert any(pid == owner.id and "отклонило" in body for pid, body in sent)
    client.get("/auth/logout")

    login(client, "chair", "pass1234")
    assert client.get(f"/documents/new?application_id={application.id}").status_code == 302
