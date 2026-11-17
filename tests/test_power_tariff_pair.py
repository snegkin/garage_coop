"""
Тариф на электроэнергию в разделе «Электроэнергия» (/power/) — одной
формой, две ставки: поставщика и для членов кооператива с одной датой
начала (app/power.py: add_tariff/edit_tariff/delete_tariff). В БД — две
записи ElectricityTariff разного kind.
"""
import datetime as dt
from decimal import Decimal

from app.models import ElectricityTariff, ElectricityTariffKind, MasterMeterReading, RoleEnum
from tests.conftest import login, make_user


def _login_board(db, client, name):
    make_user(db, name, "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, name, "pass12345")


def _rates(db):
    return sorted(
        (t.kind.value, t.rate, t.effective_date, t.comment) for t in db.query(ElectricityTariff).all()
    )


def test_add_tariff_creates_both_rates(db, client):
    _login_board(db, client, "board_tp1")
    resp = client.post("/power/tariff", data={
        "effective_date": "2026-12-01", "supplier_rate": "6,40", "member_rate": "6,60", "comment": "с декабря",
    })
    assert resp.status_code == 302
    assert _rates(db) == [
        ("member", Decimal("6.6000"), dt.date(2026, 12, 1), "с декабря"),
        ("supplier", Decimal("6.4000"), dt.date(2026, 12, 1), "с декабря"),
    ]
    # вторая пара на ту же дату не заводится
    client.post("/power/tariff", data={"effective_date": "2026-12-01", "supplier_rate": "7", "member_rate": "7"})
    assert len(_rates(db)) == 2


def test_add_tariff_requires_both_rates(db, client):
    _login_board(db, client, "board_tp2")
    client.post("/power/tariff", data={"effective_date": "2026-12-01", "supplier_rate": "6.4", "member_rate": ""})
    assert _rates(db) == []


def test_edit_and_delete_latest_pair(db, client):
    db.add_all([
        ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("5"), effective_date=dt.date(2025, 1, 1)),
        ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("5.5"), effective_date=dt.date(2025, 1, 1)),
        ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("6"), effective_date=dt.date(2026, 1, 1)),
        ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("6.5"), effective_date=dt.date(2026, 1, 1)),
    ])
    _login_board(db, client, "board_tp3")

    # более ранний тариф не правится
    client.post("/power/tariff/2025-01-01/edit", data={"effective_date": "2025-01-01", "supplier_rate": "1", "member_rate": "1"})
    db.expire_all()
    assert db.query(ElectricityTariff).filter_by(rate=Decimal("1")).count() == 0

    resp = client.post("/power/tariff/2026-01-01/edit", data={
        "effective_date": "2026-02-01", "supplier_rate": "6.1", "member_rate": "7",
    })
    assert resp.status_code == 302
    db.expire_all()
    latest = db.query(ElectricityTariff).filter_by(effective_date=dt.date(2026, 2, 1)).all()
    assert sorted((t.kind.value, t.rate) for t in latest) == [("member", Decimal("7")), ("supplier", Decimal("6.1"))]

    client.post("/power/tariff/2026-02-01/delete")
    db.expire_all()
    assert {t.effective_date for t in db.query(ElectricityTariff).all()} == {dt.date(2025, 1, 1)}


def test_used_supplier_tariff_allows_only_member_rate_change(db, client):
    supplier = ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("6"), effective_date=dt.date(2026, 1, 1))
    member = ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("6.5"), effective_date=dt.date(2026, 1, 1))
    db.add_all([supplier, member])
    db.flush()
    db.add(MasterMeterReading(year=2026, month=1, reading_date=dt.date(2026, 1, 1), reading=Decimal("100"), tariff_id=supplier.id))
    _login_board(db, client, "board_tp4")

    client.post("/power/tariff/2026-01-01/edit", data={"effective_date": "2026-01-01", "supplier_rate": "9", "member_rate": "9"})
    db.expire_all()
    assert db.get(ElectricityTariff, supplier.id).rate == Decimal("6")
    assert db.get(ElectricityTariff, member.id).rate == Decimal("6.5")

    client.post("/power/tariff/2026-01-01/edit", data={"effective_date": "2026-01-01", "supplier_rate": "6", "member_rate": "6.8"})
    db.expire_all()
    assert db.get(ElectricityTariff, member.id).rate == Decimal("6.8")

    client.post("/power/tariff/2026-01-01/delete")
    db.expire_all()
    assert db.query(ElectricityTariff).count() == 2


def test_power_page_shows_pair_in_one_row(db, client):
    db.add_all([
        ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("6.3"), effective_date=dt.date(2026, 1, 1)),
        ElectricityTariff(kind=ElectricityTariffKind.MEMBER, rate=Decimal("6.5"), effective_date=dt.date(2026, 1, 1)),
    ])
    _login_board(db, client, "board_tp5")
    html = client.get("/power/").get_data(as_text=True)
    row = next(r for r in html.split("<tr>")[1:] if "6,30 ₽" in r or "6.30 ₽" in r)
    assert "6,50 ₽" in row or "6.50 ₽" in row
    assert "действует сейчас" in row


def test_member_rate_below_supplier_plus_losses_warns(db, client):
    """Тариф для членов ниже тарифа поставщика + потери в сетях
    (ElectricitySettings.outside_line_loss_percent, по умолчанию 2,33 %) —
    предупреждение при сохранении и на странице; сохранить всё равно можно.
    Минимум — в копейках с округлением вверх: 6,30 + 2,33 % = 6,4468 → 6,45."""
    from app.accounting import member_rate_below_floor

    assert member_rate_below_floor(Decimal("6.30"), Decimal("6.44")) == Decimal("6.45")
    assert member_rate_below_floor(Decimal("6.30"), Decimal("6.45")) is None
    assert member_rate_below_floor(Decimal("6.30"), None) is None

    _login_board(db, client, "board_tp6")
    resp = client.post("/power/tariff", data={
        "effective_date": "2026-01-01", "supplier_rate": "6,30", "member_rate": "6,40",
    }, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert len(_rates(db)) == 2  # сохранено
    assert "ниже тарифа поставщика с потерями в сетях (6,45 ₽)" in html  # flash
    assert "с 01.01.2026 — 6,40 ₽, ниже тарифа поставщика с потерями в сетях (2,33%): 6,45 ₽" in html  # на странице
    assert "минимум 6,45 ₽" in html  # отметка в таблице

    # Тариф с запасом — предупреждения нет.
    resp = client.post("/power/tariff/2026-01-01/edit", data={
        "effective_date": "2026-01-01", "supplier_rate": "6,30", "member_rate": "6,50",
    }, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "с потерями в сетях (" not in html and "минимум 6,45" not in html

    # Процент потерь вырос — тот же тариф уже ниже минимума: 6,30 + 4 % = 6,552 → 6,56.
    resp = client.post("/power/outside-line-loss", data={
        "outside_line_loss_percent": "4", "meter_self_consumption_w": "2",
    }, follow_redirects=True)
    assert "ниже тарифа поставщика с потерями в сетях (6,56 ₽)" in resp.get_data(as_text=True)
