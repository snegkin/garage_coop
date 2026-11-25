"""Подстановки живых данных в вики (app/wiki_vars.py): тарифы, даты их
смены, поставщик — из базы при каждом показе, чтобы статья не устаревала."""
import datetime as dt
from decimal import Decimal

from app.models import (
    Counterparty, ElectricitySettings, ElectricityTariff, ElectricityTariffKind, RoleEnum, WikiPage,
)
from app.wiki_vars import Context, substitute

from tests.conftest import make_person, make_user, login

M, S = ElectricityTariffKind.MEMBER, ElectricityTariffKind.SUPPLIER


def _setup(db):
    supplier = Counterparty(name="ПАО «ТНС энерго Ярославль»", contract_number="15-Э",
                            contract_date=dt.date(2025, 2, 1))
    db.add(supplier)
    db.flush()
    db.add(ElectricitySettings(supplier_id=supplier.id, outside_line_loss_percent=Decimal("2.33"),
                               meter_self_consumption_w=Decimal("2")))
    db.add_all([
        ElectricityTariff(kind=M, rate=Decimal("6"), effective_date=dt.date(2025, 7, 1)),
        ElectricityTariff(kind=M, rate=Decimal("6.5"), effective_date=dt.date(2026, 10, 1)),
        ElectricityTariff(kind=S, rate=Decimal("6.3"), effective_date=dt.date(2026, 10, 1)),
        ElectricityTariff(kind=M, rate=Decimal("7.1"), effective_date=dt.date(2027, 7, 1)),
    ])
    db.flush()


def test_values_and_dates(db):
    _setup(db)
    ctx = Context(dt.date(2026, 11, 25))
    text = ("{{тариф_членам}} с {{дата_тарифа_членам}}, {{ тариф_поставщика }} с {{дата_тарифа_поставщика}}, "
            "потери {{потери_в_сети}} %, {{поставщик}}, договор {{договор_поставщика}}, "
            "далее {{новый_тариф_членам}} с {{дата_нового_тарифа_членам}}, {{новый_тариф_поставщика}}")
    assert substitute(text, ctx) == (
        "6,50 с 01.10.2026, 6,30 с 01.10.2026, потери 2,33 %, ПАО «ТНС энерго Ярославль», "
        "договор № 15-Э от 01.02.2025, далее 7,10 с 01.07.2027, —"
    )
    # Тариф берётся на дату показа: до смены — старый.
    assert substitute("{{тариф_членам}}", Context(dt.date(2026, 9, 30))) == "6,00"


def test_arithmetic_for_examples(db):
    _setup(db)
    ctx = Context(dt.date(2026, 11, 25))
    assert substitute("{{100 * тариф_членам}}", ctx) == "650,00"
    assert substitute("{{тариф_поставщика * (1 + потери_в_сети / 100)}}", ctx) == "6,45"
    assert substitute("{{(100 + 2,33 + 3 + самопотребление_счётчика*24*30/1000) * тариф_поставщика}}",
                      ctx) == "672,65"
    assert substitute("{{1000 * тариф_членам}}", ctx) == "6 500,00"


def test_unknown_and_unsafe_left_as_is(db):
    _setup(db)
    ctx = Context(dt.date(2026, 11, 25))
    for raw in ("{{опечатка}}", "{{тариф_членам + x}}", "{{__import__('os')}}", "{{поставщик * 2}}",
                "{{ item.name }}"):
        assert substitute(raw, ctx) == raw
    assert substitute("`{{тариф_членам}}` и\n```\n{{тариф_членам}}\n```\n{{тариф_членам}}", ctx) == \
        "`{{тариф_членам}}` и\n```\n{{тариф_членам}}\n```\n6,50"


def test_missing_data_dash(db):
    assert substitute("{{тариф_членам}} {{100*тариф_членам}} {{поставщик}}", Context()) == "— — —"


def test_page_shows_live_tariff(db, client):
    _setup(db)
    db.add(WikiPage(title="Расчёт", slug="raschet", body="Тариф **{{тариф_членам}} ₽**", is_internal=False))
    make_user(db, "member1", "pass1234", role=RoleEnum.MEMBER, person=make_person(db, full_name="Member One"))
    db.commit()
    login(client, "member1", "pass1234")
    body = client.get("/wiki/raschet").get_data(as_text=True)
    assert "<strong>6,50 ₽</strong>" in body
    assert "{{тариф_членам}}" not in body


def test_edit_form_lists_variables_with_current_values(db, client):
    _setup(db)
    make_user(db, "board1", "pass1234", role=RoleEnum.BOARD, person=make_person(db, full_name="Board One"))
    db.commit()
    login(client, "board1", "pass1234")
    body = client.get("/wiki/new").get_data(as_text=True)
    assert 'data-wiki-var="тариф_членам"' in body
    assert "ПАО «ТНС энерго Ярославль»" in body
