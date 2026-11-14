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
    assert {"Заявление о вступлении в кооператив", "Заявление о выходе из кооператива",
            "Заявление на подключение к электрической сети"} <= names
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


def test_member_application_templates(db, client):
    owner, garage, _chairman = _setup(db)
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    leave = db.query(DocumentTemplate).filter_by(name="Заявление о выходе из кооператива").one()
    power = db.query(DocumentTemplate).filter_by(name="Заявление на подключение к электрической сети").one()
    join = db.query(DocumentTemplate).filter_by(name="Заявление о вступлении в кооператив").one()
    # Подписывает заявитель — без подписи председателя и М.П.
    assert not (leave.show_signature or power.show_signature or join.show_signature)
    assert (join.recipient, leave.recipient, power.recipient) == ("none", "person", "garage")

    page = client.get(f"/documents/new?template_id={leave.id}&person_id={owner.id}").get_data(as_text=True)
    header = page.split('id="docAddressee"')[1].split("</div>")[0]
    assert "В правление ГК" in header and "Председателю" not in header and "Сидоров" not in header
    assert "от Петровой Анны Ивановны" in header and "/Петрова А.И./" in page and "гаража № 42" in page

    page = client.get(f"/documents/new?template_id={power.id}&garage_id={garage.id}").get_data(as_text=True)
    assert "гараж (бокс) № 42 (доля в праве 1/2)" in page

    page = client.get(f"/documents/new?template_id={join.id}").get_data(as_text=True)
    assert "Прошу принять меня в члены ГК" in page and "{{" not in page.split('id="docBody"')[1].split("</div>")[0]


def test_public_blanks_without_login(db, client):
    owner, garage, _chairman = _setup(db)
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    join = db.query(DocumentTemplate).filter_by(name="Заявление о вступлении в кооператив").one()
    leave = db.query(DocumentTemplate).filter_by(name="Заявление о выходе из кооператива").one()
    notice = db.query(DocumentTemplate).filter_by(name="Уведомление о задолженности").one()
    assert join.is_public and leave.is_public and not notice.is_public
    # Банковские реквизиты в публичном бланке — прочерком: проверяем на шаблоне с ними.
    client.post(f"/documents/templates/{notice.id}/edit", data={
        "name": notice.name, "title": notice.title, "recipient": notice.recipient, "show_number": "1",
        "show_signature": "1", "signer_role": "Председатель", "addressee_html": notice.addressee_html,
        "body_html": notice.body_html, "is_public": "1",
    })
    db.refresh(notice)
    assert notice.is_public
    client.get("/auth/logout")

    # Без входа: список (и на странице кооператива), просмотр, PDF.
    page = client.get("/documents/public/").get_data(as_text=True)
    assert "Заявление о вступлении в кооператив" in page and f"/documents/public/{leave.id}" in page
    assert f"/documents/public/{join.id}" in client.get("/cooperative/").get_data(as_text=True)
    page = client.get(f"/documents/public/{leave.id}").get_data(as_text=True)
    assert "В правление ГК" in page
    # Данные человека — прочерками, а не чьи-то.
    assert "Петрова" not in page and documents.BLANK in page
    page = client.get(f"/documents/public/{notice.id}").get_data(as_text=True)
    assert "1234567890" in page and "40703810000000000001" not in page and "044525000" not in page
    assert client.get(f"/documents/public/{leave.id}/pdf").data[:4] == b"%PDF"

    # Не общедоступный или в архиве — 404; закрытые разделы — по-прежнему с входом.
    protocol = db.query(DocumentTemplate).filter_by(name="Протокол заседания правления").one()
    assert client.get(f"/documents/public/{protocol.id}").status_code == 404
    leave.is_archived = True
    db.commit()
    assert client.get(f"/documents/public/{leave.id}/pdf").status_code == 404
    assert client.get(f"/documents/new?template_id={join.id}").status_code in (302, 401, 403)


def test_chairman_and_applicant_declension(db, app):
    owner, garage, _chairman = _setup(db)
    with app.test_request_context():
        ctx = documents.Context(dt.date(2026, 11, 14), owner, garage)
        html = documents.fill("{{председатель_кратко_кому}}|{{председатель_кому}}|от {{ФИО_от_кого}}", ctx)
    assert html == "Сидорову С.С.|Сидорову Сидору Сидоровичу|от Петровой Анны Ивановны"
    with app.test_request_context():
        assert documents.fill("{{уважаемый}} {{имя_отчество}}!", ctx) == "Уважаемая Анна Ивановна!"


def test_debt_notice_template_declension(db, client):
    owner, _garage, _chairman = _setup(db)
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    notice = db.query(DocumentTemplate).filter_by(name="Уведомление о задолженности").one()
    page = client.get(f"/documents/new?template_id={notice.id}&person_id={owner.id}").get_data(as_text=True)
    header = page.split('id="docAddressee"')[1].split("</div>")[0]
    assert "Петровой Анне Ивановне" in header
    assert "Уважаемая Анна Ивановна!" in page and "Уважаемый(ая)" not in page


def test_greeting_fallbacks():
    from app import name_declension
    assert name_declension.greeting("Кузнецов Игорь Петрович") == "Уважаемый Игорь Петрович"
    assert name_declension.greeting("Ли Мин") == "Уважаемый(ая) Ли Мин"
    assert name_declension.greeting("Смит Джон Иванс") == "Уважаемый(ая) Джон Иванс"
