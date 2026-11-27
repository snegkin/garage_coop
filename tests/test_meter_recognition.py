"""
Разбор надписей с фото счётчика (app/meter_recognition.py): номер прибора
сверяется с базой нечётко, показания выбираются по крупности цифр и
правдоподобию расхода.
"""
from decimal import Decimal

from app.meter_recognition import extract_numbers, match_meter, reading_options


def tok(text, box, score=0.99):
    return {"text": text, "score": score, "box": list(box)}


# Типичный кадр: крупный индикатор, мелкий шильдик с моделью, номером и годом.
FRAME = [
    tok("012345.6", (190, 190, 690, 320)),
    tok(" 201.5", (220, 400, 550, 465)),
    tok("No 41782563 2019", (224, 453, 573, 514)),
    tok("220V 5-60A 50Hz", (230, 507, 547, 563)),
]


def test_numbers_split_and_skip_non_numeric():
    values = {n.digits for n in extract_numbers(FRAME)}
    assert {"0123456", "2015", "41782563", "2019"} <= values
    assert not any("220" == v or "50" == v for v in values)


def test_confusable_letters_inside_number():
    numbers = extract_numbers([tok("0l2З45", (0, 0, 300, 100)), tok("Hz", (0, 200, 50, 230))])
    assert [n.digits for n in numbers] == ["012345"]


def test_integer_and_tenths_in_separate_boxes_are_merged():
    numbers = extract_numbers([tok("12345", (100, 100, 400, 200)), tok("6", (420, 110, 470, 200))])
    assert Decimal("12345.6") in {n.value for n in numbers}


def test_duplicate_from_second_pass_removed():
    numbers = extract_numbers(FRAME + FRAME)
    assert len([n for n in numbers if n.digits == "41782563"]) == 1


def test_meter_exact_and_one_digit_off():
    numbers = extract_numbers(FRAME)
    assert match_meter(numbers, [(1, "41782563"), (2, "11112222")]).meter_id == 1
    fuzzy = match_meter(numbers, [(1, "41782564"), (2, "11112222")])
    assert fuzzy.meter_id == 1 and fuzzy.score < 1


def test_meter_ambiguous_or_unknown_not_matched():
    numbers = extract_numbers(FRAME)
    # два номера, одинаково похожих на найденный — не угадываем
    assert match_meter(numbers, [(1, "41782564"), (2, "41782565")]) is None
    assert match_meter(numbers, [(1, "99990000")]) is None


def test_meter_partial_number_in_db():
    numbers = extract_numbers(FRAME)
    assert match_meter(numbers, [(1, "782563"), (2, "11112222")]).meter_id == 1


def test_reading_picks_display_and_skips_serial_and_year():
    numbers = extract_numbers(FRAME)
    options = reading_options(numbers, serial_digits="41782563", baseline=Decimal("12200"), days=60,
                              avg_daily=Decimal("2"))
    assert options[0].value == Decimal("12345.6")
    assert not options[0].warnings
    values = {o.value for o in options}
    assert Decimal("41782563") not in values and Decimal("2019") not in values


def test_tenths_reading_chosen_by_plausible_consumption():
    # «0123456» без запятой: 123456 кВт·ч — неправдоподобно после 12300,
    # 12345,6 — расход 45,6 за месяц, как обычно
    numbers = extract_numbers([tok("0123456", (100, 100, 600, 220))])
    options = reading_options(numbers, baseline=Decimal("12300"), days=30, avg_daily=Decimal("1.5"))
    assert options[0].value == Decimal("12345.6") and options[0].tenths_assumed
    assert "too_high" in next(o for o in options if o.value == Decimal("123456")).warnings


def test_reading_below_previous_flagged():
    numbers = extract_numbers([tok("1200", (100, 100, 400, 200))])
    options = reading_options(numbers, baseline=Decimal("1500"), days=30)
    assert options[0].warnings == ["not_above_previous"]


def test_no_baseline_prefers_biggest_digits():
    numbers = extract_numbers([tok("00873", (100, 100, 500, 220)), tok("1600", (100, 300, 200, 330))])
    options = reading_options(numbers)
    assert options[0].value == Decimal("873")


# --- случаи с реальных фото счётчиков (Автомотолюбитель, 27.11.2026) ---

def test_serial_with_year_suffix():
    numbers = extract_numbers([tok("No 51964723-25", (0, 0, 300, 30)), tok("32530495-17", (0, 50, 300, 80))])
    digits = {n.digits for n in numbers}
    assert "51964723" in digits and "32530495" in digits


def test_gost_lines_ignored():
    numbers = extract_numbers([tok("ГОСТ Р 52320-2005", (0, 0, 300, 30)), tok("TOCT P52322-2005", (0, 40, 300, 70)),
                               tok("FOCT 31818.11-2012", (0, 80, 300, 110))])
    assert numbers == []


def test_drum_digits_read_one_by_one_are_chained():
    # Меркурий 201: «0 0 3 4 3» и красная десятая «3» — каждая цифра отдельно
    drum = [tok(d, (500 + i * 45, 685, 535 + i * 45, 730)) for i, d in enumerate("003433")]
    options = reading_options(extract_numbers(drum), baseline=Decimal("330"), days=30)
    assert options[0].value == Decimal("343.3")


def test_nameplate_numbers_are_not_readings():
    frame = [tok("M 201", (400, 540, 630, 600)), tok("A=3200 imp/kWh", (670, 625, 780, 650)),
             tok("230B 5(60)A", (670, 610, 780, 640)), tok("2308", (670, 700, 720, 720))]
    assert reading_options(extract_numbers(frame)) == []


def test_drum_window_edge_read_as_leading_one():
    # «100019» — край окошка барабана прочитан как «1»; на деле 00019,8
    numbers = extract_numbers([tok("100019", (380, 595, 607, 687))])
    options = reading_options(numbers, baseline=Decimal("18"), days=30)
    assert int(options[0].value) == 19
