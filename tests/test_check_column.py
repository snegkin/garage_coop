"""
Галка «все» в заголовке колонки таблицы (base.html: data-check-column) —
на страницах с колонкой галок вместо кнопок «Выбрать все»/«Снять все».
Сама логика — JS (проверена в браузере), здесь — разметка страниц и
исправленная форма печати ПД-4.
"""
from decimal import Decimal

from app.models import RoleEnum, FeeType, MemberAccount, Charge

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def _board_with_debtor(db, client):
    make_user(db, "chair", "pass1234", role=RoleEnum.CHAIRMAN, person=make_person(db, full_name="Председатель П. П."))
    person = make_person(db, full_name="Должников Д. Д.")
    garage = make_garage(db, number="5")
    make_ownership(db, garage, person)
    fee = FeeType(code="membership", name="Членский взнос")
    db.add(fee)
    db.flush()
    account = MemberAccount(person_id=person.id, garage_id=garage.id, fee_type_id=fee.id, account_number="1005")
    db.add(account)
    db.flush()
    db.add(Charge(account_id=account.id, year=2025, amount=Decimal("1000")))
    db.commit()
    login(client, "chair", "pass1234")
    return account


def test_pd4_select_has_header_checkbox_and_no_nested_form(db, client):
    """Раньше «Скачать PDF» была вложенной формой со скрытыми полями ВСЕХ
    счетов — вложенный <form> браузер игнорирует, и «Печать выбранных»
    печатала всё подряд. Теперь PDF — formaction той же формы, по отмеченным."""
    _board_with_debtor(db, client)
    html = client.get("/pd4/").get_data(as_text=True)
    form = html[html.index('id="pd4Form"'):]
    form = form[:form.index("</form>")]
    assert "<form" not in form
    assert 'type="hidden" name="account_id"' not in form
    assert 'formaction="/pd4/print/pdf"' in form
    assert "data-check-column" in form


def test_setup_accounts_uses_header_checkbox(db, client):
    _board_with_debtor(db, client)
    html = client.get("/setup/accounts").get_data(as_text=True)
    assert "data-check-column" in html
    assert "accountsPickerSelectAll" not in html


def test_base_has_check_column_script(db, client):
    _board_with_debtor(db, client)
    html = client.get("/pd4/").get_data(as_text=True)
    assert "function checkColumnBoxes(" in html and "initCheckColumns(" in html
