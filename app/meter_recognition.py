"""Разбор надписей с фото счётчика: номер прибора и показания.

На входе — надписи, найденные OCR (app/meter_ocr.py: текст, уверенность,
рамка). Сама модель OCR про счётчики ничего не знает: она находит все
надписи кадра — показания, номер, год выпуска, «220V 5-60A», «imp/kWh».
Здесь из них выбирается нужное, и опора тут — база, а не модель:

* номер счётчика сверяется с номерами счётчиков, известных сайту
  (ElectricityMeter.meter_number), нечётко — одна-две ошибочные цифры не
  мешают узнать прибор, если остальные номера на него не похожи;
* показания проверяются на правдоподобие: не меньше предыдущих и расход
  за период не в разы больше обычного для этого гаража.

Окончательное значение всегда подтверждает человек (app/meter_photos.py) —
здесь только подсказка и предупреждения. Чистые функции без БД и Flask —
проверяются тестами на готовых надписях.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

# Буквы, которые OCR выдаёт вместо цифр на семисегментном индикаторе и
# шильдике. Заменяются только внутри «почти числа» (см. _numeric_part).
_CONFUSABLE = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0", "О": "0", "о": "0",
    "I": "1", "l": "1", "|": "1", "i": "1", "!": "1",
    "S": "5", "s": "5", "B": "8", "В": "8", "Z": "2", "z": "2",
    "З": "3", "з": "3", "Б": "6", "б": "6",
    ",": ".",
})
_CONFUSABLE_CHARS = {chr(k) for k in _CONFUSABLE}
# Приставки номера: «№ 123», «No123», «N 123», «Зав. №»
_NUMBER_PREFIX = re.compile(r"^(?:зав\.?)?(?:№|no\.?|n)", re.IGNORECASE)
_NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")
# Строки ГОСТа на шильдике («ГОСТ Р 52320-2005», OCR: TOCT/FOCT/rOCT) —
# номера стандартов, не показания и не номер прибора.
_GOST = re.compile(r"(?:ГОСТ|[TFrR]OCT|\bOCT)", re.IGNORECASE)
# Числа с шильдика, которые OCR находит на каждом снимке: модель
# («Меркурий 201», «231», «230»), напряжение, ток, передаточное число
# («3200 imp/kWh»), «230В», прочитанное как 2308. Показания барабана
# начинаются с нулей («00343»), поэтому числа с ведущим нулём не отсеиваются.
NAMEPLATE_VALUES = {50, 60, 201, 220, 230, 231, 236, 380, 400, 800, 1000, 1600, 2208, 2308, 3200, 6400, 32000}


@dataclass
class Number:
    """Число с фото: digits — только цифры (для сверки номера), value —
    значение с дробной частью, если на фото была точка/запятая."""
    digits: str
    value: Decimal
    text: str
    height: float
    score: float
    box: tuple
    has_point: bool = False


@dataclass
class MeterMatch:
    meter_id: int
    meter_number: str
    score: float
    found: str


@dataclass
class ReadingOption:
    value: Decimal
    text: str
    score: float
    warnings: list[str] = field(default_factory=list)
    tenths_assumed: bool = False


def digits_only(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _numeric_part(part: str) -> str | None:
    """«012З45,6» → «012345.6»; None — если это не число («220V», «50Hz»)."""
    part = _NUMBER_PREFIX.sub("", part.strip()).strip(" :.-")
    if not part:
        return None
    # Замены только там, где цифр большинство — иначе «Hz» и «kWh» тоже
    # превратились бы в числа.
    real_digits = sum(ch.isdigit() for ch in part)
    digit_like = sum(ch.isdigit() or ch in _CONFUSABLE_CHARS for ch in part)
    if real_digits == 0 or digit_like < 0.8 * len(part) or real_digits < 0.5 * len(part):
        return None
    norm = part.translate(_CONFUSABLE)
    norm = norm.strip(".")
    if norm.count(".") > 1:
        norm = norm.replace(".", "")
    return norm if _NUMERIC.match(norm) else None


def _iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def extract_numbers(tokens: list[dict]) -> list[Number]:
    """Все числа с фото. Надпись «No 41782563 2019» даёт два числа; две
    соседние надписи в одной строке («012345» и маленькая «6» справа — так
    часто выглядят целая и дробная части индикатора) дают ещё и склеенное
    «012345.6». Повторы (второй проход OCR по контрастному кадру) убираются."""
    numbers: list[Number] = []
    for t in tokens:
        if _GOST.search(str(t["text"])):
            continue
        box = tuple(t["box"])
        height = max(1.0, box[3] - box[1])
        # «16146698-13», «51964723-25» — номер и год выпуска через дефис
        parts = [p for word in str(t["text"]).split() for p in re.split(r"(?<=\d)-(?=\d)", word)]
        for part in parts:
            norm = _numeric_part(part)
            if norm is None:
                continue
            numbers.append(Number(
                digits=digits_only(norm), value=Decimal(norm), text=part,
                height=height, score=float(t.get("score", 0)), box=box, has_point="." in norm,
            ))

    # Склейка целой и дробной части: одна строка, дробная правее и рядом.
    merged = []
    for a in numbers:
        for b in numbers:
            if a is b or a.has_point or b.has_point or len(b.digits) > 2:
                continue
            overlap = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
            gap = b.box[0] - a.box[2]
            if overlap > 0.4 * min(a.height, b.height) and -0.2 * a.height <= gap <= 1.2 * a.height:
                merged.append(Number(
                    digits=a.digits + b.digits, value=Decimal(f"{a.digits}.{b.digits}"),
                    text=f"{a.text} {b.text}", height=a.height, score=min(a.score, b.score),
                    box=(a.box[0], min(a.box[1], b.box[1]), b.box[2], max(a.box[3], b.box[3])),
                    has_point=True,
                ))
    numbers += merged
    numbers += _drum_chains([n for n in numbers if not n.has_point and len(n.digits) <= 3])

    unique: list[Number] = []
    for n in sorted(numbers, key=lambda n: -n.score):
        if any(u.value == n.value and u.digits == n.digits and _iou(u.box, n.box) > 0.5 for u in unique):
            continue
        unique.append(n)
    return unique


def _drum_chains(short: list[Number]) -> list[Number]:
    """Барабанный счётчик OCR часто читает по одной цифре на окошко
    («0», «0», «3», «4», «3»): цепочки коротких чисел одной высоты в одну
    строку, вплотную друг к другу, склеиваются в одно число («00343»)."""
    short = sorted(short, key=lambda n: n.box[0])

    def next_of(a):
        best = None
        for b in short:
            if b is a:
                continue
            gap = b.box[0] - a.box[2]
            overlap = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
            h = max(a.height, b.height)
            if (-0.3 * h <= gap <= 1.5 * h and overlap > 0.5 * min(a.height, b.height)
                    and 0.6 <= a.height / b.height <= 1.6 and (best is None or b.box[0] < best.box[0])):
                best = b
        return best

    has_prev = {id(b) for a in short if (b := next_of(a)) is not None}
    chains = []
    for start in short:
        if id(start) in has_prev:
            continue
        chain, cur = [start], next_of(start)
        while cur is not None and cur not in chain and len(chain) < 9:
            chain.append(cur)
            cur = next_of(cur)
        digits = "".join(n.digits for n in chain)
        if len(chain) >= 2 and len(digits) >= 4:
            chains.append(Number(
                digits=digits, value=Decimal(digits), text=" ".join(n.text for n in chain),
                height=max(n.height for n in chain), score=min(n.score for n in chain),
                box=(chain[0].box[0], min(n.box[1] for n in chain), chain[-1].box[2], max(n.box[3] for n in chain)),
            ))
    return chains


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _number_similarity(found: str, known: str) -> float:
    if not found or not known:
        return 0.0
    if found == known:
        return 1.0
    shorter, longer = sorted((found, known), key=len)
    # В базе бывает записан не весь номер (последние цифры) или OCR
    # прихватил соседние цифры (год выпуска в той же строке).
    if len(shorter) >= 5 and (longer.endswith(shorter) or longer.startswith(shorter)):
        return 0.9
    return 1 - _levenshtein(found, known) / len(longer)


# Номер узнан, если похож хотя бы так и заметно лучше любого другого:
# 8 цифр с одной ошибкой — 0,875; двумя — 0,75 (не узнаём).
MATCH_MIN = 0.8
MATCH_MARGIN = 0.1


def match_meter(numbers: list[Number], meters: list[tuple[int, str]]) -> MeterMatch | None:
    """Какой из известных счётчиков на фото. meters — [(id, номер)].
    Только номера от 4 цифр: короче — совпадения случайны."""
    scored = []
    for meter_id, meter_number in meters:
        known = digits_only(meter_number)
        if len(known) < 4:
            continue
        best, found = 0.0, ""
        for n in numbers:
            if len(n.digits) < 4:
                continue
            s = _number_similarity(n.digits, known)
            if s > best:
                best, found = s, n.digits
        scored.append((best, meter_id, meter_number, found))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < MATCH_MIN:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MATCH_MARGIN:
        return None
    best, meter_id, meter_number, found = scored[0]
    return MeterMatch(meter_id=meter_id, meter_number=meter_number, score=round(best, 3), found=found)


def _looks_like_year(n: Number) -> bool:
    return not n.has_point and len(n.digits) == 4 and 1980 <= int(n.digits) <= 2040


def reading_options(
    numbers: list[Number],
    *,
    serial_digits: str | None = None,
    baseline: Decimal | None = None,
    days: int | None = None,
    avg_daily: Decimal | None = None,
) -> list[ReadingOption]:
    """Кандидаты в показания, лучший первым.

    serial_digits — номер счётчика, найденный на фото (не путать с
    показаниями); baseline — предыдущие показания; days — дней с них;
    avg_daily — обычный расход гаража, кВт·ч/сутки (по прошлым показаниям).

    Счёт: крупные цифры (индикатор — самая крупная надпись кадра) и
    правдоподобный расход. Без точки на фото у числа от 6 цифр есть и
    второе прочтение — последняя цифра десятые (так показывают Меркурий,
    Нева, барабанные с красным последним разрядом); какое из двух верно,
    решает расход относительно предыдущих показаний."""
    candidates = [
        n for n in numbers
        if 3 <= len(n.digits) <= 9 and not _looks_like_year(n)
        and not (int(n.value) in NAMEPLATE_VALUES and not n.digits.startswith("0"))
        and not (serial_digits and len(n.digits) >= 5 and _number_similarity(n.digits, serial_digits) >= MATCH_MIN)
    ]
    if not candidates:
        return []
    max_height = max(n.height for n in candidates)

    # Сколько разумно набежать с предыдущих: впятеро больше обычного,
    # но не меньше 30 кВт·ч в сутки (обогреватель зимой) и 300 в целом.
    limit = None
    expected = None
    if baseline is not None and days is not None:
        days = max(days, 1)
        if avg_daily is not None and avg_daily > 0:
            expected = avg_daily * days
        limit = max(Decimal(300), Decimal(30) * days, (expected or 0) * 5)

    options: dict[Decimal, ReadingOption] = {}
    for n in candidates:
        variants = [(n.value, False)]
        if not n.has_point:
            digit_variants = [n.digits]
            # Край окошка барабана слева OCR читает как «1»: «100019» —
            # на деле «00019» (+ красная десятая).
            if len(n.digits) >= 6 and n.digits.startswith("10"):
                digit_variants.append(n.digits[1:])
                variants.append((Decimal(n.digits[1:]), False))
            for d in digit_variants:
                if len(d) >= 5:
                    variants.append(((Decimal(d) / 10).quantize(Decimal("0.1")), True))
        for value, tenths in variants:
            score = n.height / max_height + 0.3 * n.score
            warnings = []
            if tenths:
                score -= 0.05  # при равенстве — как написано на фото
            if baseline is not None:
                delta = value - baseline
                if delta <= 0:
                    score -= 2
                    warnings.append("not_above_previous")
                elif limit is not None and delta > limit:
                    score -= 1
                    warnings.append("too_high")
                else:
                    score += 1
                    if expected:
                        # ближе к обычному расходу — лучше (до +0,3)
                        score += 0.3 / (1 + abs(float(delta - expected)) / float(expected))
            text = n.text
            prev = options.get(value)
            if prev is None or score > prev.score:
                options[value] = ReadingOption(
                    value=value, text=text, score=round(score, 3), warnings=warnings, tenths_assumed=tenths,
                )
    return sorted(options.values(), key=lambda o: -o.score)
