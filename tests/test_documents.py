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
    AuditLog, BankAccount, Charge, Cooperative, DocumentTemplate, ElectricityMeter, ElectricityReading,
    ElectricitySettings, FeeType, IssuedDocument, MemberAccount, RoleEnum,
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
    assert db.query(AuditLog).filter(AuditLog.action.like("document_template.%")).count() == 0  # заготовки — не в аудит

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
    # Выход — по Уставу на имя председателя (п. 5.6), не раньше двух недель.
    assert "Председателю правления ГК «Восход»" in header and "Сидорову С.С." in header
    assert "от Петровой Анны Ивановны" in header and "/Петрова А.И./" in page and "гаража № 42" in page
    assert "не ранее чем через две недели" in page

    page = client.get(f"/documents/new?template_id={power.id}&garage_id={garage.id}").get_data(as_text=True)
    assert "гараж (бокс) № 42 (доля в праве 1/2)" in page

    page = client.get(f"/documents/new?template_id={join.id}").get_data(as_text=True)
    assert "Прошу принять меня в члены ГК" in page and "{{" not in page.split('id="docBody"')[1].split("</div>")[0]


def test_blank_after_pipe(db, app):
    owner, garage, _chairman = _setup(db)
    with app.test_request_context():
        ctx = documents.Context(dt.date(2026, 11, 16), owner, garage)
        missing = set()
        html = documents.fill("<p>{{паспорт|____|______}} {{ФИО|___}} {{ опечатка|__ }}</p>", ctx, missing)
        assert html == "<p>____<br>______ Петрова Анна Ивановна {{ опечатка|__ }}</p>" and missing == {"паспорт"}
        assert documents.fill_text("{{паспорт|____|__}}", ctx) == "____ __"
    assert documents.unknown_placeholders("{{паспорт|___}} {{опечатка|_}}") == ["опечатка|_"]


def test_power_contracts(db, client):
    owner, garage, _chairman = _setup(db)
    meter = ElectricityMeter(garage_id=garage.id, meter_number="СЕ101-777", meter_seal_number="П-15",
                             installed_date=dt.date(2025, 5, 1), initial_reading=Decimal("100.00"))
    db.add(meter)
    db.add(ElectricitySettings(outside_line_loss_percent=Decimal("2.50")))
    db.flush()
    db.add(ElectricityReading(meter_id=meter.id, reading=Decimal("1234.50"), reading_date=dt.date(2026, 10, 25)))
    db.commit()
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    outside = db.query(DocumentTemplate).filter_by(name="Договор на электроэнергию (не член кооператива)").one()
    member = db.query(DocumentTemplate).filter_by(name="Договор на электроэнергию (член кооператива)").one()
    # Подписи обеих Сторон — в тексте, подпись председателя бланка выключена; номер — у документа.
    assert not (outside.show_signature or member.show_signature) and outside.show_number and member.show_number
    assert not (outside.for_members or member.for_members or outside.is_public)

    page = client.get(f"/documents/new?template_id={outside.id}&garage_id={garage.id}").get_data(as_text=True)
    body = page.split('id="docBody"')[1].split("</div>")[0]
    assert "{{" not in body
    assert "Петрова Анна Ивановна, владелец гаража (бокса) № 42" in body and "именуемая в дальнейшем" in body
    assert "ИПУ) № СЕ101-777" in body and "1234,5 кВт·ч" in body and "— 2,5% от объёма" in body
    assert "1,46 кВт·ч в месяц" in body and "/Петрова А.И./" in body and "/Сидоров С.С./" in body
    assert "Пользователь не является членом" in body and "экземплярах" not in body

    assert "мощностью 4 кВт" in body and "{{год}}" not in outside.body_html
    # Срок оплаты — не задан в реквизитах кооператива: прочерк; пени нет, «15 дней» нет.
    assert "Срок оплаты — до ________ года, следующего за расчётным" in body
    assert "Пени за просрочку оплаты электроэнергии не начисляются" in body and "15 (пятнадцать)" not in body
    coop = db.query(Cooperative).one()
    coop.dues_due_day, coop.dues_due_month = 31, 3
    db.commit()
    page = client.get(f"/documents/new?template_id={outside.id}&garage_id={garage.id}").get_data(as_text=True)
    assert "Срок оплаты — до 31 марта года, следующего за расчётным" in page
    assert "до 31 марта каждого года" in page and "после предупреждения Пользователя" in page
    # Лицевой счёт гаража — в реквизитах и в способах оплаты; записи счёта нет — по формуле номера.
    from app.accounting import electricity_account_number
    from app.models import PersonalAccount
    number = electricity_account_number(garage.id)
    assert f"Лицевой счёт № {number}</p>" in page and f"лицевого счёта № {number};" in page
    db.add(PersonalAccount(garage_id=garage.id, account_number="E-42"))
    db.commit()
    page = client.get(f"/documents/new?template_id={outside.id}&garage_id={garage.id}").get_data(as_text=True)
    assert "Лицевой счёт № E-42</p>" in page
    # Пустые реквизиты — прочерк до края колонки, адрес — в две строки.
    assert f"Дата рождения: {'_' * 20}<br>" in body and f"Тел.: {'_' * 29}<br>Лицевой счёт № " in body
    owner.registration_address = None
    db.commit()
    page = client.get(f"/documents/new?template_id={outside.id}&garage_id={garage.id}").get_data(as_text=True)
    assert f"Адрес регистрации: {'_' * 16}<br>{'_' * 34}<br>" in page

    page = client.get(f"/documents/new?template_id={member.id}&garage_id={garage.id}").get_data(as_text=True)
    body = page.split('id="docBody"')[1].split("</div>")[0]
    assert "{{" not in body and "доля в праве 1/2" in body and "пломба № П-15" in body
    assert "<b>Член кооператива</b>" in body and "3.3.2. не превышать" in body


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
    assert "Председателю правления ГК" in page
    assert "Председателю правления ГК" in client.get(f"/documents/public/{join.id}").get_data(as_text=True)
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


def test_header_blanks_and_phones_one_per_line(db, app):
    from app.models import Phone
    _owner, _garage, _chairman = _setup(db)
    nobody = make_person(db, full_name="Иванов Иван Иванович")
    two_phones = make_person(db, full_name="Смирнов Олег Петрович", residence_address="г. Город")
    db.add_all([Phone(person_id=two_phones.id, number="+79001112233"),
                Phone(person_id=two_phones.id, number="+79004445566")])
    db.commit()
    header = "<p>паспорт: {{паспорт}}</p><p>адрес: {{адрес}}</p><p>телефон: {{телефон}}</p>"
    with app.test_request_context():
        ctx = documents.Context(dt.date(2026, 11, 16), nobody)
        html = documents.fill(header, ctx, header=True)
        assert f"паспорт: {'_' * 22}</p>" in html
        assert f"адрес: {'_' * 24}<br>{'_' * 24}</p>" in html
        assert f"телефон: {'_' * 24}</p>" in html
        assert documents.fill("<p>от {{ФИО_от_кого}}</p>", documents.Context(dt.date(2026, 1, 1)), header=True) \
            == f"<p>от {'_' * 24}</p>"
        # В тексте документа — обычный короткий прочерк.
        assert documents.fill("{{адрес}}", ctx) == documents.BLANK

        ctx = documents.Context(dt.date(2026, 11, 16), two_phones)
        assert "телефон: +79001112233<br>+79004445566</p>" in documents.fill(header, ctx, header=True)
        assert documents.fill("{{телефон}}", ctx) == "+79001112233, +79004445566"


def test_member_application_text_from_template(db, client, app):
    owner, garage, _chairman = _setup(db)
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    members = {t.name for t in documents.member_templates()}
    assert members == {"Заявление о выходе из кооператива", "Заявление на подключение к электрической сети",
                       "Жалоба", "Обращение"}
    leave = db.query(DocumentTemplate).filter_by(name="Заявление о выходе из кооператива").one()
    complaint = db.query(DocumentTemplate).filter_by(name="Жалоба").one()
    with app.test_request_context():
        heading, title, body = documents.member_application_text(leave, owner, garage, dt.date(2026, 11, 16))
        assert (heading, title) == ("Заявление", "о выходе из кооператива")
        # Подпись заявителя — своя у заявления, строка из шаблона убрана.
        assert "гаража № 42" in body and "Петрова А.И." not in body
        heading, title, _body = documents.member_application_text(complaint, owner, garage, dt.date(2026, 11, 16))
        assert (heading, title) == ("Жалоба", "")


def test_sale_contract(db, client):
    owner, garage, chairman = _setup(db)
    buyer = make_person(db, full_name="Козлов Пётр Сергеевич", registration_address="г. Город, ул. Новая, 5")
    make_user(db, "buyer", "pass1234", role=RoleEnum.MEMBER, person=buyer)
    whole = make_garage(db, number="7", cadastral_number="61:44:0000000:7")
    make_ownership(db, whole, owner)
    db.commit()
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    sale = db.query(DocumentTemplate).filter_by(code=documents.SALE_CONTRACT_CODE).one()
    assert sale.recipient == "sale" and sale.chairman_only and sale.show_signature and sale.show_number
    # Пустой бланк — без входа, с прочерками вместо сторон.
    assert sale.is_public
    client.get("/auth/logout")
    blank = client.get(f"/documents/public/{sale.id}").get_data(as_text=True)
    assert '<p class="print-title">ДОГОВОР</p>' in blank and "купли-продажи гаража</p>" in blank and "Петрова" not in blank and "_" * 40 in blank
    login(client, "board1", "pass1234")
    # Оформляет только председатель.
    assert client.get(f"/documents/new?template_id={sale.id}").status_code == 302
    client.get("/logout")
    login(client, "chair", "pass1234")

    # Обе стороны не из реестра — бланк с прочерками, без «не заполнено».
    page = client.get(f"/documents/new?template_id={sale.id}").get_data(as_text=True)
    body = page.split('id="docBody"')[1].split("</div>")[0]
    assert "{{" not in body and "Не заполнены в реестре" not in page
    assert "_" * 40 + ", именуемый(ая) в дальнейшем «Продавец»" in body
    assert "покупает гараж (бокс) № ______ " in body and "/Сидоров С.С./" in page

    # Продавец и покупатель из реестра.
    url = f"/documents/new?template_id={sale.id}&owner={owner.id}:{whole.id}&buyer_id={buyer.id}"
    page = client.get(url).get_data(as_text=True)
    body = page.split('id="docBody"')[1].split("</div>")[0]
    assert "Петрова Анна Ивановна, именуемая в дальнейшем «Продавец»" in body
    assert "Козлов Пётр Сергеевич, именуемый в дальнейшем «Покупатель»" in body
    assert "покупает гараж (бокс) № 7 " in body and "61:44:0000000:7" in body
    assert "/Петрова А.И./" in body and "/Козлов П.С./" in body and "г. Город, ул. Новая, 5" in body
    assert "перед Кооперативом: 0,00 руб." in body and "нотариальному" not in page
    assert "полностью выплатившему паевой взнос" in body and "п. 5.5 Устава" in body
    assert "Супруг(а) Продавца" in body

    # Покупатель не из реестра, гараж — без продавца.
    page = client.get(f"/documents/new?template_id={sale.id}&garage_id={whole.id}").get_data(as_text=True)
    assert "покупает гараж (бокс) № 7 " in page and "счётчика гаража" in page and "Покупатель: ФИО" not in page
    # Доля в праве — предупреждение о нотариусе.
    page = client.get(f"/documents/new?template_id={sale.id}&owner={owner.id}:{garage.id}").get_data(as_text=True)
    assert "покупает 1/2 доли в праве собственности на гараж (бокс) № 42" in page and "нотариальному" in page

    resp = client.post("/documents/new", data={
        "template_id": sale.id, "owner": f"{owner.id}:{whole.id}", "garage_id": "", "buyer_id": buyer.id,
        "issued_at": "2026-11-17", "title": "ДОГОВОР купли-продажи гаража", "body_html": "<p>Текст</p>",
    })
    assert resp.status_code == 302
    doc = db.query(IssuedDocument).one()
    assert (doc.person_id, doc.garage_id, doc.buyer_person_id) == (owner.id, whole.id, buyer.id)
    assert "Козлов" in db.query(AuditLog).filter_by(action="issued_document.create").one().summary
    # Договор видит и покупатель.
    client.get("/logout")
    login(client, "buyer", "pass1234")
    assert client.get(f"/documents/{doc.id}").status_code == 200


def test_contractor_letters(db, client):
    from app.models import Counterparty
    _owner, _garage, _chairman = _setup(db)
    host = Counterparty(name="ООО «Хостинг»", inn="7800000000", address="г. Санкт-Петербург, ул. Серверная, 1",
                        email="support@host.example")
    db.add(host)
    db.commit()
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    letter = db.query(DocumentTemplate).filter_by(name="Письмо контрагенту").one()
    removal = db.query(DocumentTemplate).filter_by(
        name="Заявление об удалении учётной записи (личного кабинета)").one()
    assert letter.recipient == removal.recipient == "contractor" and removal.show_signature

    # Без контрагента — только выбор.
    page = client.get(f"/documents/new?template_id={removal.id}").get_data(as_text=True)
    assert 'id="docBody"' not in page and "ООО «Хостинг» (ИНН 7800000000)" in page
    page = client.get(f"/documents/new?template_id={removal.id}&counterparty_id={host.id}").get_data(as_text=True)
    head = page.split('id="docAddressee"')[1].split("</div>")[0]
    body = page.split('id="docBody"')[1].split("</div>")[0]
    assert "ООО «Хостинг»" in head and "ИНН 7800000000" in head and "support@host.example" in head
    assert "{{" not in body and "ИНН 1234567890" in body and "40703810000000000001" in body
    assert "председателя правления Сидоров Сидор Сидорович" in body

    # Карточка контрагента — ссылки на шаблоны писем; сохранённое письмо — в списке.
    card = client.get(f"/counterparties/{host.id}").get_data(as_text=True)
    assert f"template_id={removal.id}&amp;counterparty_id={host.id}" in card and "Писем пока нет" in card
    resp = client.post("/documents/new", data={
        "template_id": removal.id, "counterparty_id": host.id, "issued_at": "2026-11-17",
        "title": "ЗАЯВЛЕНИЕ об удалении учётной записи", "body_html": "<p>Прошу удалить</p>",
    })
    doc = db.query(IssuedDocument).one()
    assert resp.status_code == 302 and doc.counterparty_id == host.id and doc.number
    card = client.get(f"/counterparties/{host.id}").get_data(as_text=True)
    assert f"/documents/{doc.id}" in card and "Писем пока нет" not in card
    assert "ООО «Хостинг»" in client.get(f"/documents/{doc.id}").get_data(as_text=True)


def test_split_doc_title():
    split = documents.split_doc_title
    assert split("ДОГОВОР о подключении гаража") == ("ДОГОВОР", "о подключении гаража")
    assert split("Ответ на заявление о выходе") == ("ОТВЕТ", "на заявление о выходе")
    assert split("ПРОТОКОЛ № ___ заседания правления") == ("ПРОТОКОЛ № ___", "заседания правления")
    assert split("ЗАЯВЛЕНИЕ") == ("ЗАЯВЛЕНИЕ", "")
    assert split("КООПЕРАТИВ В ИНТЕРНЕТЕ") == ("КООПЕРАТИВ В ИНТЕРНЕТЕ", "")
    assert split("  ") == ("", "")


def test_document_title_on_two_lines(db, client):
    owner, garage, _chairman = _setup(db)
    login(client, "board1", "pass1234")
    client.post("/documents/templates/defaults")
    member = db.query(DocumentTemplate).filter_by(name="Договор на электроэнергию (член кооператива)").one()
    page = client.get(f"/documents/new?template_id={member.id}&garage_id={garage.id}").get_data(as_text=True)
    body = page.split('id="docBody"')[1].split("</div>")[0]
    client.post("/documents/new", data={
        "template_id": member.id, "owner": f"{owner.id}:{garage.id}", "issued_at": "2026-11-17",
        "title": member.title, "body_html": "<p>Текст.</p>",
    })
    document = db.query(IssuedDocument).one()
    page = client.get(f"/documents/{document.id}").get_data(as_text=True)
    assert '<p class="print-title">ДОГОВОР</p>' in page
    assert '<p class="print-subtitle">о подключении гаража к электрической сети кооператива</p>' in page
