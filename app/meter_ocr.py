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
    img = ImageOps.exif_transpose(img).convert("RGB")
    scale = OCR_MAX_SIDE / max(img.size)
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img, exif_date


def _run(engine, img):
    import numpy as np

    result = engine(np.asarray(img)[:, :, ::-1])  # RapidOCR ждёт BGR, как OpenCV
    tokens = []
    if result is None or result.txts is None:
        return tokens
    for text, score, box in zip(result.txts, result.scores, result.boxes):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        tokens.append({
            "text": str(text), "score": round(float(score), 3),
            "box": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))],
        })
    return tokens


def recognize(path):
    from PIL import ImageOps
    from rapidocr import RapidOCR

    img, exif_date = _load_image(path)
    engine = RapidOCR(params={"Global.log_level": "error"})
    tokens = _run(engine, img)
    # ЖК-индикатор под стеклом часто бледный: если в первом проходе нет
    # ни одного числа от 5 цифр (ни показаний, ни номера), второй — по
    # контрастному серому кадру. Не всегда: проход — ещё 2–3 с на сервере.
    # Дубли (тот же текст в том же месте) разбирает app/meter_recognition.py.
    if not any(sum(ch.isdigit() for ch in t["text"]) >= 5 for t in tokens):
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
