#!/usr/bin/env python3
"""
Выгрузка фото счётчиков с подтверждёнными показаниями — обучающий набор
для распознавания (app/meter_photos.py, модель MeterPhoto).

Каждое фото, по которому человек подтвердил показания, — готовый
размеченный пример: снимок и верные цифры (показания из ElectricityReading,
номер прибора из ElectricityMeter), рядом — что предложило распознавание.
По расхождениям видно, где оно ошибается, а набор годится для дообучения
модели под счётчики кооператива.

Запуск (как остальные скрипты — с окружением кооператива):
    INSTANCE_DIR=... venv/bin/python scripts/export_meter_photos.py КАТАЛОГ

В КАТАЛОГ кладутся фото (имя — id фото) и labels.csv:
    file, photo_id, date, garage, meter_number, reading,
    suggested_reading, suggested_meter_number, reading_ok, meter_ok
Персональных данных нет: только номера гаражей и счётчиков.
"""
import csv
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    out_dir = argv[1]
    os.makedirs(out_dir, exist_ok=True)

    from app import create_app, database
    from app.models import MeterPhoto

    app = create_app()
    with app.app_context():
        photos = (
            database.db_session.query(MeterPhoto)
            .filter(MeterPhoto.reading_id.isnot(None))
            .order_by(MeterPhoto.id)
            .all()
        )
        rows = 0
        with open(os.path.join(out_dir, "labels.csv"), "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "file", "photo_id", "date", "garage", "meter_number", "reading",
                "suggested_reading", "suggested_meter_number", "reading_ok", "meter_ok",
            ])
            for photo in photos:
                src = os.path.join(app.config["UPLOAD_FOLDER"], photo.file_path)
                if not os.path.isfile(src):
                    continue
                name = f"{photo.id}{os.path.splitext(photo.file_path)[1]}"
                shutil.copyfile(src, os.path.join(out_dir, name))
                reading, meter = photo.reading, photo.meter
                writer.writerow([
                    name, photo.id, reading.reading_date.isoformat(),
                    photo.garage.number if photo.garage else "",
                    meter.meter_number if meter else "", reading.reading,
                    photo.suggested_reading if photo.suggested_reading is not None else "",
                    photo.suggested_meter_number or "",
                    int(photo.suggested_reading is not None and photo.suggested_reading == reading.reading),
                    int(bool(meter and photo.suggested_meter_number == meter.meter_number)),
                ])
                rows += 1
    print(f"{rows} фото → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
