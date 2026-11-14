"""
«Информация о кооперативе» без входа: открытые сведения видны, баланс,
комментарий, расчётный счёт, площади/кадастр, документы и адрес
председателя (подставляемый вместо почтового) — нет.
"""
import datetime as dt
from decimal import Decimal

from app.models import BankAccount, Cooperative, Document, DocumentType, RoleEnum

from tests.conftest import make_person, make_user, login


def _setup(db):
    db.add(Cooperative(
        full_name="Гаражный кооператив «Восход»", short_name="ГК «Восход»", inn="1234567890", kpp="123401001",
        ogrn="1023400000000", legal_address="г. Город, ул. Гаражная, 1", phone="+7 900 000-00-00",
        email="coop@example.org", website="https://voskhod.example", social_links="Telegram: https://t.me/voskhod",
        comment="секретный комментарий", cadastral_value=Decimal("9876543.21"),
    ))
    db.add(BankAccount(bank_name="АО «Банк»", bik="044525000", checking_account="40703810000000000001",
                       is_primary=True))
    chairman = make_person(db, full_name="Сидоров Сидор Сидорович", is_chairman=True,
                           residence_address="г. Город, ул. Домашняя, 5, кв. 7")
    make_user(db, "member", "pass1234", role=RoleEnum.MEMBER, person=make_person(db, full_name="Член Кооператива"))
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=chairman)
    db.add(Document(doc_type=list(DocumentType)[0], title="Устав кооператива", date=dt.date(2020, 1, 1),
                    is_internal=False))
    db.commit()


def test_anonymous_sees_only_public_fields(db, client):
    _setup(db)
    resp = client.get("/cooperative/")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    for visible in ("1234567890", "123401001", "1023400000000", "ул. Гаражная, 1", "+7 900 000-00-00",
                    "coop@example.org", "https://voskhod.example", "https://t.me/voskhod"):
        assert visible in page, visible
    for hidden in ("40703810000000000001", "044525000", "секретный комментарий", "9 876 543,21", "9876543",
                   "ул. Домашняя", "Устав кооператива", "Баланс", "Площади и налоги", "Добавить документ"):
        assert hidden not in page, hidden
    # Ссылка в шапке для анонимного.
    assert 'href="/cooperative/"' in client.get("/").get_data(as_text=True)


def test_member_still_sees_bank_account_and_documents(db, client):
    _setup(db)
    login(client, "member", "pass1234")
    page = client.get("/cooperative/").get_data(as_text=True)
    assert "40703810000000000001" in page and "Устав кооператива" in page and "Площади и налоги" in page
    assert "Баланс" not in page and "секретный комментарий" not in page
