"""
Тесты автоматического расчёта земельного налога (accounting.compute_land_tax).

Формула:
- net_taxable_area = common_area (текущая площадь на кадастровой карте)
- total_tax = cadastral_area × (land_tax_rate_percent / 100)
- price_per_sqm = total_tax / common_area
- common_area_tax = price_per_sqm × (common_area / кол-во_гаражей)
- неприватизированный: (standard_garage_land_area × price_per_sqm + common_area_tax) × (1 + bank_fee_percent/100)
- приватизированный: (common_area_tax минус стоимость превышения фактической
  приватизированной площади над стандартной, если больше; иначе 0) × (1 + bank_fee_percent/100)
"""
from decimal import Decimal

from app import database
from app.accounting import compute_land_tax
from app.models import Cooperative, Garage, GarageOwnership, FeeType, Person, MemberAccount, Charge

from tests.conftest import make_person, make_garage


def _make_coop(db, **kwargs):
    coop = Cooperative(
        full_name="Тестовый кооператив",
        inn="1234567890",
        kpp="123456789",
        ogrn="1234567890123",
        total_area=kwargs.get("total_area", Decimal("1000")),
        common_area=kwargs.get("common_area", Decimal("500")),
        cadastral_area=kwargs.get("cadastral_area", Decimal("800")),
        cadastral_value=kwargs.get("cadastral_value", Decimal("5000000")),
        land_tax_rate_percent=kwargs.get("land_tax_rate_percent", Decimal("1.5")),
        standard_garage_land_area=kwargs.get("standard_garage_land_area", Decimal("30")),
        bank_fee_percent=kwargs.get("bank_fee_percent", Decimal("1.6")),
    )
    db.add(coop)
    db.flush()
    return coop


def test_returns_none_when_no_coop(app, db):
    """Нет кооператива — возвращает None."""
    result = compute_land_tax(2026)
    assert result is None


def test_returns_none_when_missing_cadastral_area_field(app, db):
    """Не указана кадастровая площадь — возвращает None."""
    _make_coop(db, cadastral_area=None)
    result = compute_land_tax(2026)
    assert result is None


def test_returns_none_when_missing_cadastral_value(app, db):
    """Не указана кадастровая стоимость — возвращает None."""
    _make_coop(db, cadastral_value=None)
    result = compute_land_tax(2026)
    assert result is None


def test_returns_none_when_no_common_area(app, db):
    """Не указана общая площадь — возвращает None."""
    _make_coop(db, common_area=None)
    result = compute_land_tax(2026)
    assert result is None


def test_returns_empty_dict_when_no_garages(app, db):
    """Гаражей нет — возвращает пустой словарь."""
    _make_coop(db)
    result = compute_land_tax(2026)
    assert result == {}


def test_single_non_privatized_garage(app, db):
    """Один гараж, не приватизирован — полная формула с комиссией банка и коэффициентом."""
    coop = _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None
    assert garage.id in result

    # Проверка промежуточных значений:
    # total_tax = 5000000 × 1.5% = 75000
    # price_per_sqm = 75000 / 800 = 93.75
    # common_area_tax = 93.75 × (500 / 1) = 46875
    # under_building = 30 × 93.75 = 2812.50
    # garage_tax = (2812.50 + 46875) × 1.016 = 49687.50 × 1.016 = 50482.50
    # коэффициент по умолчанию = 1, поэтому без изменений
    expected = (Decimal("2812.50") + Decimal("46875")) * Decimal("1.016")
    assert result[garage.id] == expected.quantize(Decimal("0.01"))


def test_land_tax_with_coefficient(app, db):
    """Коэффициент гаража умножает сумму налога."""
    coop = _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", coefficient=Decimal("2"))

    result = compute_land_tax(2026)
    assert result is not None

    # base = (2812.50 + 46875) × 1.016 = 50482.50
    # с коэффициентом 2: 50482.50 × 2 = 100965.00
    expected = ((Decimal("2812.50") + Decimal("46875")) * Decimal("1.016")) * Decimal("2")
    assert result[garage.id] == expected.quantize(Decimal("0.01"))


def test_land_tax_with_coefficient_less_than_one(app, db):
    """Коэффициент < 1 (маленький гараж)."""
    coop = _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", coefficient=Decimal("0.5"))

    result = compute_land_tax(2026)
    assert result is not None

    # base = 50482.50, с коэффициентом 0.5: 25241.25
    expected = ((Decimal("2812.50") + Decimal("46875")) * Decimal("1.016")) * Decimal("0.5")
    assert result[garage.id] == expected.quantize(Decimal("0.01"))


def test_single_privatized_garage(app, db):
    """Один гараж, приватизирован — common_area_tax с комиссией банка (тем
    же % за зачисление платежа, что и у неприватизированных — не зависит
    от приватизации участка)."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", land_privatized=True, privatized_land_area=Decimal("30"))

    result = compute_land_tax(2026)
    assert result is not None

    # total_tax = 5000000 × 1.5% = 75000
    # price_per_sqm = 75000 / 800 = 93.75
    # common_area_tax = 93.75 × (500 / 1) = 46875
    # × 1.016 (% банка) = 47625
    expected = (Decimal("46875") * Decimal("1.016")).quantize(Decimal("0.01"))
    assert result[garage.id] == expected


def test_multiple_garages_shared_cost(app, db):
    """Два гаража — общая стоимость делится пропорционально."""
    _make_coop(db)
    g1 = make_garage(db, number="1", area_sqm="18.00")
    g2 = make_garage(db, number="2", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None

    # price_per_sqm = 93.75, common_area_tax = 93.75 × (500 / 2) = 23437.50
    # under_building = 30 × 93.75 = 2812.50
    # каждый гараж: (2812.50 + 23437.50) × 1.016 = 26250 × 1.016 = 26670
    expected = (Decimal("2812.50") + Decimal("23437.50")) * Decimal("1.016")
    assert result[g1.id] == expected.quantize(Decimal("0.01"))
    assert result[g2.id] == expected.quantize(Decimal("0.01"))


def test_mixed_privatized_and_not(app, db):
    """Смешанные: один приватизирован, другой нет."""
    _make_coop(db)
    g1 = make_garage(db, number="1", area_sqm="18.00", land_privatized=True, privatized_land_area=Decimal("30"))
    g2 = make_garage(db, number="2", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None

    # g1 (приватизирован): common_area_tax × 1.016 = 93.75 × (500/2) × 1.016 = 23812.50
    # g2 (не приватизирован): (2812.50 + 23437.50) × 1.016 = 26670
    expected_g1 = (Decimal("23437.50") * Decimal("1.016")).quantize(Decimal("0.01"))
    assert result[g1.id] == expected_g1
    expected_g2 = (Decimal("2812.50") + Decimal("23437.50")) * Decimal("1.016")
    assert result[g2.id] == expected_g2.quantize(Decimal("0.01"))


def test_zero_bank_fee_no_multiplier(app, db):
    """При bank_fee_percent=0 множитель равен 1."""
    _make_coop(db, bank_fee_percent=Decimal("0"))
    garage = make_garage(db, number="1", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None

    expected = Decimal("2812.50") + Decimal("46875")
    assert result[garage.id] == expected.quantize(Decimal("0.01"))


def test_different_standard_area(app, db):
    """Кастомная стандартная площадь под гараж."""
    _make_coop(db, standard_garage_land_area=Decimal("24"))
    garage = make_garage(db, number="1", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None

    # under_building = 24 × 93.75 = 2250
    expected = (Decimal("2250") + Decimal("46875")) * Decimal("1.016")
    assert result[garage.id] == expected.quantize(Decimal("0.01"))


def test_different_cadastral_value(app, db):
    """Разная кадастровая стоимость влияет на результат пропорционально."""
    _make_coop(db, cadastral_value=Decimal("10000000"))  # в 2 раза больше
    garage = make_garage(db, number="1", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None

    # total_tax = 10000000 × 1.5% = 150000
    # price_per_sqm = 150000 / 800 = 187.5
    # common_area_tax = 187.5 × 500 = 93750
    # under_building = 30 × 187.5 = 5625
    expected = (Decimal("5625") + Decimal("93750")) * Decimal("1.016")
    assert result[garage.id] == expected.quantize(Decimal("0.01"))


def test_year_is_ignored(app, db):
    """Год не используется в расчёте (в отличие от старой версии с LandTaxYear)."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00")

    result_2025 = compute_land_tax(2025)
    result_2026 = compute_land_tax(2026)
    result_2030 = compute_land_tax(2030)

    assert result_2025 == result_2026 == result_2030


def test_quantization_to_two_decimals(app, db):
    """Результат всегда округлён до двух знаков."""
    _make_coop(db, cadastral_value=Decimal("3892792.20"))
    garage = make_garage(db, number="1", area_sqm="18.00")

    result = compute_land_tax(2026)
    assert result is not None
    # Проверка: результат — Decimal с precision 0.01
    quantized = result[garage.id].quantize(Decimal("0.01"))
    assert result[garage.id] == quantized


def test_privatized_garage_with_excess_area_deducts_cost(app, db):
    """Приватизированная площадь больше стандартной (30) — стоимость
    превышения вычитается из доли в общей территории, а не суммируется
    поверх (в отличие от неприватизированного гаража)."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", land_privatized=True, privatized_land_area=Decimal("40"))

    result = compute_land_tax(2026)
    assert result is not None

    # common_area_tax = 93.75 × (500/1) = 46875
    # excess_area = 40 - 30 = 10; excess_cost = 10 × 93.75 = 937.50
    # (46875 - 937.50) × 1.016 (% банка) = 45937.50 × 1.016 = 46672.50
    expected = ((Decimal("46875") - Decimal("937.50")) * Decimal("1.016")).quantize(Decimal("0.01"))
    assert result[garage.id] == expected


def test_privatized_garage_excess_area_clamped_to_zero(app, db):
    """Если стоимость превышения площади больше самой доли в общей
    территории — начисление обнуляется, а не уходит в минус."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", land_privatized=True, privatized_land_area=Decimal("1000"))

    result = compute_land_tax(2026)
    assert result is not None
    # excess_cost = (1000-30) × 93.75 = 90937.50 >> common_area_tax 46875 — обнуляется
    # (0 × 1.016 (% банка) всё равно 0)
    assert result[garage.id] == Decimal("0.00")


def test_privatized_garage_area_equal_to_standard_no_deduction(app, db):
    """Приватизированная площадь РОВНО стандартная — превышения нет,
    вычитать нечего (граничный случай, не строгое >)."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", land_privatized=True, privatized_land_area=Decimal("30"))

    result = compute_land_tax(2026)
    assert result is not None
    expected = (Decimal("46875") * Decimal("1.016")).quantize(Decimal("0.01"))
    assert result[garage.id] == expected


def test_privatized_garage_without_area_set_no_deduction(app, db):
    """privatized_land_area не заполнено (None) — поведение как раньше,
    без вычета (не с чем сравнивать стандартную площадь)."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00", land_privatized=True)

    result = compute_land_tax(2026)
    assert result is not None
    expected = (Decimal("46875") * Decimal("1.016")).quantize(Decimal("0.01"))
    assert result[garage.id] == expected


def test_no_ownerships_still_calculates(app, db):
    """Расчёт работает даже если у гаража нет собственников."""
    _make_coop(db)
    garage = make_garage(db, number="1", area_sqm="18.00")
    # Без GarageOwnership и MemberAccount

    result = compute_land_tax(2026)
    assert result is not None
    assert garage.id in result
