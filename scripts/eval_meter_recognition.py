#!/usr/bin/env python3
"""
Точность распознавания показаний по фото на размеченном наборе.

Набор — каталог в формате scripts/export_meter_photos.py: фото и
labels.csv (нужны колонки file, meter_number, reading; пустая ячейка —
на фото не видно). Каждое фото распознаётся заново (app/meter_ocr.py),
разбирается (app/meter_recognition.py) так же, как на сайте: номер
сверяется со всеми номерами набора, показания — относительно
«предыдущих», условно на 3 % меньше верных (как при ежемесячном обходе).

Запуск:
    venv/bin/python scripts/eval_meter_recognition.py КАТАЛОГ [--python PY] [--cache КАТАЛОГ]

--python — интерпретатор с rapidocr (по умолчанию текущий); --cache —
куда сохранять/откуда брать результаты OCR (повторный прогон после
правки разбора — без OCR, за секунды).
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from decimal import Decimal, InvalidOperation

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from app.meter_recognition import extract_numbers, match_meter, reading_options  # noqa: E402

OCR_SCRIPT = os.path.join(PROJECT_DIR, "app", "meter_ocr.py")


def _ocr(path, python, cache_dir):
    cached = os.path.join(cache_dir, os.path.basename(path) + ".json") if cache_dir else None
    if cached and os.path.isfile(cached):
        with open(cached, encoding="utf-8") as fh:
            return json.load(fh)
    proc = subprocess.run([python, "-I", OCR_SCRIPT, path], capture_output=True, text=True, timeout=300)
    out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {"error": proc.stderr[-300:]}
    if cached and "tokens" in out:
        with open(cached, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--cache")
    args = ap.parse_args()
    if args.cache:
        os.makedirs(args.cache, exist_ok=True)

    with open(os.path.join(args.dataset, "labels.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    known = sorted({r["meter_number"] for r in rows if r.get("meter_number")})
    meters = [(i, number) for i, number in enumerate(known)]

    serial_ok = serial_total = top = top_int = total = 0
    for r in rows:
        out = _ocr(os.path.join(args.dataset, r["file"]), args.python, args.cache)
        numbers = extract_numbers(out.get("tokens", []))
        line = [r["file"]]
        if r.get("meter_number"):
            serial_total += 1
            m = match_meter(numbers, meters)
            hit = m is not None and m.meter_number == r["meter_number"]
            serial_ok += hit
            line.append(f"номер {'да' if hit else 'нет'}")
        try:
            truth = Decimal(r["reading"]) if r.get("reading") else None
        except InvalidOperation:
            truth = None
        if truth is not None:
            total += 1
            baseline = (truth * Decimal("0.97")).quantize(Decimal("0.1")) if truth > 50 else Decimal(0)
            options = reading_options(numbers, serial_digits=r.get("meter_number") or None,
                                      baseline=baseline, days=30)
            best = options[0].value if options else None
            top += best == truth
            top_int += best is not None and int(best) == int(truth)
            line.append(f"показания {truth} → {best if best is not None else '—'}")
        if out.get("error"):
            line.append(f"ошибка OCR: {out['error']}")
        print("  ".join(line))

    print()
    print(f"Номер счётчика: {serial_ok} из {serial_total}")
    print(f"Показания: точно {top} из {total}, целая часть {top_int} из {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
