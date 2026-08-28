"""
- Вкладка «Информация» страницы гаража показывает сводку по всем его
  лицевым счетам (электричество + MemberAccount каждого собственника) —
  см. app/garages.py: detail(), account_summary_rows.
- Приватность: рядовой собственник видит в этой сводке только СВОИ
  MemberAccount (can_view_member_account), не счета содольщиков.
- Фильтр на странице документов — селектор, не кнопки (app/templates/documents/list.html).
"""
from decimal import Decimal
import datetime as dt

from app.models import RoleEnum, FeeType, MemberAccount, PersonalAccount, Charge, DocumentType, Document
from app import database

from tests.conftest import make_person, make_garage, make_ownership, make_user, login


def test_garage_detail_shows_electricity_account_balance(app, db, client):
    person = make_person(db, full_name="Электричество Тестович")
    garage = make_garage(db, number="101")
    make_ownership(db, garage, person)
    db.add(PersonalAccount(garage_id=garage.id, account_number="10101"))
    make_user(db, "board_elec", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_elec", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "10101" in html
    assert "Электричество" in html or "Electricity" in html


def test_garage_detail_owner_sees_own_member_account_balance(app, db, client):
    person = make_person(db, full_name="Владелец Один Одинович")
    garage = make_garage(db, number="102")
    make_ownership(db, garage, person)
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    member_account = MemberAccount(
        person_id=person.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="20102",
    )
    db.add(member_account)
    db.flush()
    db.add(Charge(account_id=member_account.id, year=2026, amount=Decimal("500.00")))
    make_user(db, "owner1", "pass12345", role=RoleEnum.MEMBER, person=person)
    db.commit()
    login(client, "owner1", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "20102" in html
    assert "500,00" in html or "500.00" in html


def test_garage_detail_hides_co_owner_member_account_balance(app, db, client):
    """Ключевая проверка приватности: на одном гараже два содольщика с
    отдельными MemberAccount (взносы зависят от доли) — каждый видит
    только свой счёт, не счёт другого."""
    owner_a = make_person(db, full_name="Совладелец Алексей Алексеевич")
    owner_b = make_person(db, full_name="Совладелец Борис Борисович")
    garage = make_garage(db, number="103")
    make_ownership(db, garage, owner_a, share="0.5")
    make_ownership(db, garage, owner_b, share="0.5")
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    db.add(MemberAccount(person_id=owner_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="30103"))
    db.add(MemberAccount(person_id=owner_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="30104"))
    make_user(db, "coowner_a", "pass12345", role=RoleEnum.MEMBER, person=owner_a)
    db.commit()
    login(client, "coowner_a", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "30103" in html       # свой счёт виден
    assert "30104" not in html   # счёт содольщика — нет


def test_garage_detail_board_sees_all_member_accounts(app, db, client):
    owner_a = make_person(db, full_name="Правленец Смотрит Всё")
    owner_b = make_person(db, full_name="Совладелец Второй Вторович")
    garage = make_garage(db, number="104")
    make_ownership(db, garage, owner_a, share="0.5")
    make_ownership(db, garage, owner_b, share="0.5")
    fee_type = FeeType(code="10", name="Членский взнос")
    db.add(fee_type)
    db.flush()
    db.add(MemberAccount(person_id=owner_a.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="40105"))
    db.add(MemberAccount(person_id=owner_b.id, garage_id=garage.id, fee_type_id=fee_type.id, account_number="40106"))
    make_user(db, "board_all", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board_all", "pass12345")

    resp = client.get(f"/garages/{garage.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "40105" in html
    assert "40106" in html


# ---------------------------------------------------------------------------
# Документы — фильтр селектором, не кнопками
# ---------------------------------------------------------------------------

def test_documents_list_filter_is_a_select(app, db, client):
    make_user(db, "docuser", "pass12345", role=RoleEnum.MEMBER)
    db.commit()
    login(client, "docuser", "pass12345")

    resp = client.get("/documents/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<select name="type"' in html
    assert 'onchange="this.form.submit()"' in html
    # старых кнопок-фильтров быть не должно
    assert "btn-outline-secondary\" href=\"{{ url_for('documents.list_documents', type=" not in html


def test_documents_list_filter_selects_current_type(app, db, client):
    make_user(db, "docuser2", "pass12345", role=RoleEnum.MEMBER)
    db.add(Document(
        doc_type=DocumentType.PROTOCOL, date=__import__("datetime").date(2026, 1, 1),
        title="Протокол №1",
    ))
    db.add(Document(
        doc_type=DocumentType.CHARTER, date=__import__("datetime").date(2026, 1, 1),
        title="Устав",
    ))
    db.commit()
    login(client, "docuser2", "pass12345")

    resp = client.get("/documents/", query_string={"type": DocumentType.PROTOCOL.value})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'value="{DocumentType.PROTOCOL.value}" selected' in html
    assert "Протокол №1" in html
    assert "Устав" not in html
