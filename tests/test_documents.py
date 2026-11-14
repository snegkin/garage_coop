"""
Бланки и шаблоны документов (app/documents.py): подстановки из реестра,
стандартные шаблоны, редактирование шаблона, составление документа по
шаблону с номером и без, журнал (вместе со справками о владении),
PDF/печать, доступ только правлению.
"""
import datetime as dt
from decimal import Decimal

from app import documents
from app.models import (
    AuditLog, BankAccount, Charge, Cooperative, DocumentTemplate, FeeType, IssuedDocument,
    MemberAccount, RoleEnum,
)

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _setup(db):
    db.add(Cooperative(
        full_name="Гаражный кооператив «Восход»", short_name="ГК «Восход»", inn="1234567890", kpp="123401001",
        ogrn="1023400000000", legal_address="г. Город, ул. Гаражная, 1", website="https://voskhod.example",
        phone="+7 900 000-00-00", social_links="Telegram: https://t.me/voskhod\nVK: https://vk.com/voskhod",
    ))
    db.add(BankAccount(bank_name="АО «Банк»", bik="044525000", checking_account="40703810000000000001",
                       correspondent_account="30101810000000000000", is_primary=True))
    owner = make_person(db, full_name="Петрова Анна Ивановна", registration_address="г. Город, ул. Улица, 1")
    garage = make_garage(db, number="42", cadastral_number="61:44:0000000:1")
    make_ownership(db, garage, owner, share="0.5")
    chairman = make_person(db, full_name="Сидоров Сидор Сидорович", is_chairman=True)
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman)
    board = make_person(db, full_name="Член Правления")
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=board)
    make_user(db, "owner", "pass1234", role=RoleEnum.MEMBER, person=owner)
    db.commit()
    return owner, garage, chairman


def test_fill_placeholders(db, app):
    owner, garage, _chairman = _setup(db)
    fee_type = FeeType(code="membership", name="Членский взнос", type_code="1")
    db.add(fee_type)
    db.flush()
    account = MemberAccount(person_id=owner.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="10420")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2026, amount=Decimal("1500.5")))
    db.commit()

    with app.test_request_context():
        ctx = documents.Context(dt.date(2026, 11, 13), owner, garage)
        missing = set()
        html = documents.fill(
            "<p>{{ФИО_кому}}, гараж {{гараж}}, доля {{доля}}, долг {{задолженность}}, счёт {{лицевые_счета}}, "
            "ИНН {{ инн }}, р/с {{расчётный_счёт}}, {{сегодня}}, {{паспорт}}, {{опечатка}}</p><p>{{соцсети}}</p>",
            ctx, missing,
        )
    assert "Петровой Анне Ивановне, гараж 42, доля 1/2" in html
    assert "долг 1 500,50" in html and "счёт 10420" in html
    assert "ИНН 1234567890" in html and "р/с 40703810000000000001" in html and "13.11.2026" in html
    # Пустое значение — прочерк и в список незаполненных; неизвестный ключ — как есть.
    assert f", {documents.BLANK}, {{{{опечатка}}}}" in html and missing == {"паспорт"}
    # Многострочное значение — через <br>, с экранированием.
    assert "Telegram: https://t.me/voskhod<br>VK: https://vk.com/voskhod" in html
    assert documents.unknown_placeholders(html) == ["опечатка"]

    with app.test_request_context():
        bad = make_person(db, full_name="<b>Хакер</b>")
        assert "&lt;b&gt;" in documents.fill("{{ФИО}}", documents.Context(dt.date(2026, 1, 1), bad))


def test_defaults_and_announcement_without_number(db, client):
    _setup(db)
    login(client, "board1", "pass1234")
    page = client.get("/documents/").get_data(as_text=True)
    assert "Добавить стандартные шаблоны" in page and "Справка о владении гаражом" in page

    client.post("/documents/templates/defaults")
    names = {t.name for t in db.query(DocumentTemplate)}
    assert {"Справка о владении гаражом", "Объявление об общем собрании", "Сайт и группы в соцсетях", "Регистрационные данные и реквизиты",
            "Уведомление о задолженности"} <= names
    cert = db.query(DocumentTemplate).filter_by(name="Справка о владении гаражом").one()
    assert cert.code == "garage_certificate" and cert.chairman_only
    client.post("/documents/templates/defaults")
    assert db.query(DocumentTemplate).count() == len(names)

    info = db.query(DocumentTemplate).filter_by(name="Сайт и группы в соцсетях").one()
    page = client.get(f"/documents/new?template_id={info.id}").get_data(as_text=True)
    assert "https://voskhod.example" in page and "https://t.me/voskhod" in page and 'id="docBody"' in page

    resp = client.post("/documents/new", data={
        "template_id": info.id, "issued_at": "2026-11-13", "title": "МЫ В ИНТЕРНЕТЕ",
        "body_html": "<p>Сайт https://voskhod.example</p><script>x</script>", "addressee_html": "",
    })
    assert resp.status_code == 302
    document = db.query(IssuedDocument).one()
    assert document.number is None and document.title == "МЫ В ИНТЕРНЕТЕ" and "<script" not in document.body_html
    assert not document.show_signature

    page = client.get(f"/documents/{document.id}").get_data(as_text=True)
    assert "МЫ В ИНТЕРНЕТЕ" in page and "ИНН 1234567890" in page and "тел. +7 900 000-00-00" in page


def test_template_editor_and_numbered_document_for_owner(db, client):
    owner, garage, _chairman = _setup(db)
    login(client, "board1", "pass1234")
    page = client.get("/documents/templates/new").get_data(as_text=True)
    assert "{{ФИО_кому}}" in page and 'data-insert-var="ИНН"' in page

    client.post("/documents/templates/new", data={
        "name": "Разрешение на въезд", "title": "РАЗРЕШЕНИЕ", "recipient": "garage", "show_number": "1",
        "show_signature": "1", "signer_role": "Председатель",
        "addressee_html": "<p>{{ФИО}}</p>", "body_html": "<p>Гараж № {{гараж}}, {{кооператив_кратко}}. {{нет_такого}}</p>",
    })
    template = db.query(DocumentTemplate).one()
    assert template.recipient == "garage" and "{{гараж}}" in template.body_html
    assert db.query(AuditLog).filter_by(action="document_template.create").count() == 1

    # Без адресата — только выбор собственника.
    page = client.get(f"/documents/new?template_id={template.id}").get_data(as_text=True)
    assert "Собственник и гараж" in page and 'id="docBody"' not in page
    resp = client.post("/documents/new", data={"template_id": template.id, "issued_at": "2026-11-13",
                                               "body_html": "<p>x</p>"})
    assert resp.status_code == 200 and db.query(IssuedDocument).count() == 0

    page = client.get(f"/documents/new?template_id={template.id}&garage_id={garage.id}").get_data(as_text=True)
    assert "Гараж № 42, ГК «Восход»" in page.replace("&laquo;", "«").replace("&raquo;", "»") or "Гараж № 42" in page
    assert "Петрова Анна Ивановна" in page

    for _ in range(2):
        client.post("/documents/new", data={
            "template_id": template.id, "owner": f"{owner.id}:{garage.id}", "issued_at": "2026-11-13",
            "title": "РАЗРЕШЕНИЕ", "addressee_html": "<p>Петрова Анна Ивановна</p>", "body_html": "<p>Гараж № 42</p>",
        })
    docs = db.query(IssuedDocument).order_by(IssuedDocument.id).all()
    assert [d.number for d in docs] == ["2026-1", "2026-2"]
    assert docs[0].person_id == owner.id and docs[0].garage_id == garage.id

    # Копия документа — тот же текст без повторной подстановки.
    page = client.get(f"/documents/new?copy_of={docs[0].id}").get_data(as_text=True)
    assert "Гараж № 42" in page and 'id="docBody"' in page

    page = client.get("/documents/").get_data(as_text=True)
    assert "2026-2" in page and "Разрешение на въезд" in page

    client.post(f"/documents/{docs[1].id}/delete")
    assert db.query(IssuedDocument).count() == 1
    client.post(f"/documents/templates/{template.id}/archive")
    db.refresh(template)
    assert template.is_archived


def test_access_board_only(db, client):
    _setup(db)
    login(client, "owner", "pass1234")
    assert client.get("/documents/").status_code in (302, 403)
    assert client.get("/documents/templates/new").status_code in (302, 403)


def test_title_placeholders_and_date_shift(db, client, app):
    owner, garage, _chairman = _setup(db)
    with app.test_request_context():
        ctx = documents.Context(dt.date(2026, 11, 14), owner, garage)
        assert documents.fill("<p>до {{сегодня+10}}, {{ сегодня - 14 }}, {{сегодня+0}}</p>", ctx) == \
            "<p>до 24.11.2026, 31.10.2026, 14.11.2026</p>"
        assert documents.unknown_placeholders("{{сегодня+30}} {{сегодня*2}}") == ["сегодня*2"]
        # Заголовок — обычный текст: без HTML-экранирования (его делает шаблон).
        assert documents.fill_text("Правление {{кооператив_кратко}} — {{ФИО}}", ctx) == \
            "Правление ГК «Восход» — Петрова Анна Ивановна"

    login(client, "board1", "pass1234")
    client.post("/documents/templates/new", data={
        "name": "Уведомление", "title": "Правление {{кооператив_кратко}} уведомляет!", "recipient": "person",
        "show_number": "1", "show_signature": "1", "signer_role": "Председатель",
        "body_html": "<p>Оплатить до {{сегодня+10}}.</p>",
    })
    template = db.query(DocumentTemplate).one()
    page = client.get(f"/documents/new?template_id={template.id}&person_id={owner.id}&issued_at=2026-11-14")
    page = page.get_data(as_text=True)
    assert "{{кооператив_кратко}}" not in page and "Правление ГК" in page
    assert "Оплатить до 24.11.2026." in page
    # Другая дата документа — срок пересчитан.
    page = client.get(f"/documents/new?template_id={template.id}&person_id={owner.id}&issued_at=2026-12-01")
    assert "Оплатить до 11.12.2026." in page.get_data(as_text=True)
