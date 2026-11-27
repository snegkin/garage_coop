"""Распознавание надписей на фото счётчика — отдельным процессом.

Запускается как скрипт (``python -I app/meter_ocr.py фото.jpg``) из
app/meter_photos.py, а не импортируется в процесс сайта: модель OCR
(RapidOCR — PaddleOCR в формате ONNX, ~30 МБ) занимает в памяти ~250 МБ,
а на сервере в 2 ГБ работают несколько кооперативов, у каждого свой
процесс gunicorn. Отдельный процесс отдаёт память сразу после снимка и не
роняет сайт, если библиотека или модель сломаются.

Здесь только стандартная библиотека, Pillow и rapidocr — без Flask и без
пакета app (скрипт запускается с -I, пакет ему недоступен).

Вывод — одна строка JSON в stdout:
    {"tokens": [{"text", "score", "box": [x0, y0, x1, y1]}, ...],
     "width", "height", "exif_date": "ГГГГ-ММ-ДД" | null}
Ошибка — код возврата 2 и {"error": "..."}.

``--warmup`` — только скачать модели (при деплое, см. warmup()).
"""
import json
import os
import re
import sys

# Длинная сторона кадра для OCR: мельче — теряется номер на шильдике,
# крупнее — дольше без выигрыша (детектор всё равно уменьшает кадр).
OCR_MAX_SIDE = 1600


def _load_image(path):
    from PIL import Image, ImageOps

    img = Image.open(path)
    exif_date = None
    try:
        # DateTimeOriginal (0x9003) — в Exif IFD, DateTime (0x0132) — в основном
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)
        if raw and len(raw) >= 10:
            exif_date = raw[:10].replace(":", "-")
    except Exception:
        exif_date = None
    full = ImageOps.exif_transpose(img).convert("RGB")
    img = full
    scale = OCR_MAX_SIDE / max(full.size)
    if scale < 1:
        img = full.resize((round(full.width * scale), round(full.height * scale)), Image.LANCZOS)
    return full, img, exif_date


def _run(engine, img, offset=(0, 0), k=1.0, zoom=False):
    """Надписи кадра; offset/k — пересчёт рамок увеличенного фрагмента
    обратно в координаты основного кадра."""
    import numpy as np

    result = engine(np.asarray(img)[:, :, ::-1])  # RapidOCR ждёт BGR, как OpenCV
    tokens = []
    if result is None or result.txts is None:
        return tokens
    for text, score, box in zip(result.txts, result.scores, result.boxes):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        token = {
            "text": str(text), "score": round(float(score), 3),
            "box": [round(offset[0] + min(xs) * k), round(offset[1] + min(ys) * k),
                    round(offset[0] + max(xs) * k), round(offset[1] + max(ys) * k)],
        }
        if zoom:
            token["zoom"] = True
        tokens.append(token)
    return tokens


# Таблички, рядом с которыми стоит счётный механизм: модель Меркурия
# («МЕРКУРИЙ 201» над барабаном) и «kW·h»/«кВт·ч» у барабана Лейне, СО.
_MODEL_ANCHOR = re.compile(r"(?:^|\D)(?:201|231|230|236)(?:\D|$)")
_KWH_ANCHOR = re.compile(r"k\s*W|кВт|KBT", re.IGNORECASE)
ZOOM_WIDTH = 1200
ZOOM_MAX_CROPS = 3


def _zoom_regions(tokens, width, height):
    """Где искать барабан крупнее: на снимке в 1600 px цифры барабана —
    20–40 px, их часто не находит детектор. Под табличкой модели и вокруг
    «kW·h» вырезается фрагмент из полного кадра и распознаётся отдельно
    (проверено на фото счётчиков Автомотолюбителя: Меркурий 201/231, Лейне,
    СО-И449)."""
    regions = []
    for t in tokens:
        x0, y0, x1, y1 = t["box"]
        w, h = x1 - x0, y1 - y0
        if _MODEL_ANCHOR.search(t["text"]) and h > 15 and w > 2 * h:
            regions.append((x0 - 0.3 * w, y0 + 0.3 * h, x1 + 0.6 * w, y1 + 2.2 * h))
        elif _KWH_ANCHOR.search(t["text"]):
            regions.append((x0 - 0.3 * width, y0 - 0.06 * height, x1 + 0.12 * width, y1 + 0.08 * height))
    result = []
    for r in regions:
        r = (max(0, r[0]), max(0, r[1]), min(width, r[2]), min(height, r[3]))
        if r[2] - r[0] < 20 or r[3] - r[1] < 20:
            continue
        if any(abs(r[0] - o[0]) < 30 and abs(r[1] - o[1]) < 30 for o in result):
            continue
        result.append(r)
    return result[:ZOOM_MAX_CROPS]


def recognize(path):
    from PIL import ImageOps
    from rapidocr import RapidOCR

    from PIL import Image

    full, img, exif_date = _load_image(path)
    engine = RapidOCR(params={"Global.log_level": "error"})
    tokens = _run(engine, img)
    regions = _zoom_regions(tokens, img.width, img.height)
    scale = full.width / img.width
    for r in regions:
        crop = full.crop(tuple(round(v * scale) for v in r))
        k = ZOOM_WIDTH / crop.width
        crop = crop.resize((ZOOM_WIDTH, max(1, round(crop.height * k))), Image.LANCZOS)
        tokens += _run(engine, crop, offset=(r[0], r[1]), k=(r[2] - r[0]) / ZOOM_WIDTH, zoom=True)
    # Ни таблички, ни длинных чисел (бледный ЖК под стеклом) — проход по
    # контрастному серому кадру. Дубли (тот же текст в том же месте)
    # разбирает app/meter_recognition.py.
    if not regions and not any(sum(ch.isdigit() for ch in t["text"]) >= 5 for t in tokens):
        contrast = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=2).convert("RGB")
        tokens += _run(engine, contrast)
    return {"tokens": tokens, "width": img.width, "height": img.height, "exif_date": exif_date}


def warmup():
    """Скачать модели заранее. rapidocr при первом запуске кладёт их в свой
    каталог в venv, а сайты кооперативов работают от пользователей без
    права записи туда — поэтому деплой зовёт это от владельца venv
    (deployment/deploy.sh, ansible tasks/code.yml)."""
    from rapidocr import RapidOCR

    RapidOCR(params={"Global.log_level": "error"})
    print(json.dumps({"ok": True}))
    return 0


def main(argv):
    if argv[1:] == ["--warmup"]:
        return warmup()
    if len(argv) != 2 or not os.path.isfile(argv[1]):
        print(json.dumps({"error": "usage: meter_ocr.py IMAGE"}))
        return 2
    try:
        out = recognize(argv[1])
    except Exception as e:  # noqa: BLE001 — любой сбой отдаём сайту текстом
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
