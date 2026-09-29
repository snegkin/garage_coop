import datetime as dt
from decimal import Decimal

from app.models import ElectricityTariff, ElectricityTariffKind, RoleEnum
from tests.conftest import login, make_user


def test_future_tariff_does_not_mark_current_as_ended(client, db):
    """Будущий тариф не должен помечаться «действует сейчас», а текущий —
    наоборот, должен, даже если у него уже есть дата окончания."""
    today = dt.date.today()
    current = ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("5.11"),
                                effective_date=today - dt.timedelta(days=30))
    future = ElectricityTariff(kind=ElectricityTariffKind.SUPPLIER, rate=Decimal("6.22"),
                               effective_date=today + dt.timedelta(days=2))
    db.add_all([current, future])
    make_user(db, "board", "pw12345678", role=RoleEnum.BOARD)
    db.commit()
    login(client, "board", "pw12345678")

    html = client.get("/power/").get_data(as_text=True)
    rows = [r.split("</tr>")[0] for r in html.split("<tr>")[1:]]
    future_row = next(r for r in rows if "6,22 ₽" in r or "6.22 ₽" in r)
    current_row = next(r for r in rows if "5,11 ₽" in r or "5.11 ₽" in r)

    assert "ещё не вступил в силу" in future_row
    assert "действует сейчас" not in future_row
    assert "действует сейчас" in current_row
