"""Фото гаражей с обхода территории → галерея гаражей (GaragePhoto).

    INSTANCE_DIR=/path/instance venv/bin/python scripts/import_garage_photos.py MAP.csv PHOTOS_DIR \
        [--create=1-298,51а,269А] [--dry-run]

MAP.csv — таблица «снимок → гараж» (колонки file, garage, kind), её собирают
по обходу вручную вместе с председателем; хранится в каталоге экземпляра
кооператива (INSTANCE_DIR/garage_map/), не в репозитории.
Берутся строки kind:
  anchor — номер прочитан на воротах или назван председателем;
  calc   — номер досчитан по порядку между опорами (подпись об этом);
  letter — «176а?»: бокс, видимо, с литерой — фото кладётся к основному
           номеру с подписью.
Номер с «?» (пристройка, неточный) — фото с подписью «номер не точен».
Остальное (чужие гаражи, соседний ГСК, общие планы, углы) пропускается.

--create — заводит гаражи, которых ещё нет в БД (площадь-заглушка 24 м²,
лицевой счёт ЭЭ, как при создании гаража на сайте): диапазоны и номера
через запятую.

Повторный запуск не дублирует: гаражи, у которых фото уже есть, пропускаются.
Файл копируется в UPLOAD_FOLDER под случайным именем и ужимается
(app.images.shrink_image), как при загрузке через сайт.
"""
import csv
import os
import re
import shutil
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database, audit  # noqa: E402
from app.accounting import electricity_account_number  # noqa: E402
from app.images import shrink_image  # noqa: E402
from app.models import Garage, GaragePhoto, PersonalAccount, User  # noqa: E402

DEFAULT_AREA = Decimal("24")  # площадь неизвестна — заглушка, поправить в карточке гаража
CAPTION_CALC = "Номер по порядку в ряду, на воротах не указан"
CAPTION_FUZZY = "Номер не точен"
CAPTION_LETTER = "Возможно, отдельный бокс с литерой"


def parse_create(spec: str) -> list[str]:
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            out += [str(n) for n in range(int(m[1]), int(m[2]) + 1)]
        else:
            out.append(part)
    return out


def photo_target(garage: str, kind: str):
    """(номер гаража, подпись) или None, если снимок в галерею не идёт."""
    if kind not in ("anchor", "calc", "letter"):
        return None
    if kind == "letter":
        m = re.fullmatch(r"(\d+)[а-я]\?", garage)
        return (m[1], CAPTION_LETTER) if m else None
    m = re.fullmatch(r"(\d+[а-яА-Я]?)(\?)?", garage)
    if not m:
        return None  # «107–108?» и т.п. — неоднозначно
    if m[2]:
        return m[1], CAPTION_FUZZY
    return m[1], (CAPTION_CALC if kind == "calc" else None)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)
    dry = "--dry-run" in sys.argv
    create = []
    for a in sys.argv[1:]:
        if a.startswith("--create="):
            create = parse_create(a.split("=", 1)[1])
    map_path, photos_dir = args

    rows = list(csv.DictReader(open(map_path, encoding="utf-8")))
    plan = {}  # номер → [(файл, подпись)]
    for r in rows:
        t = photo_target(r["garage"], r["kind"])
        if t:
            plan.setdefault(t[0], []).append((r["file"], t[1]))
    for files in plan.values():
        for fn, _ in files:
            if not os.path.isfile(os.path.join(photos_dir, fn)):
                sys.exit(f"Нет файла {fn} в {photos_dir}")

    app = create_app()
    copied = []
    with app.test_request_context():
        from flask import g
        s = database.db_session
        upload_folder = app.config["UPLOAD_FOLDER"]
        garages = {gr.number: gr for gr in s.query(Garage).all()}

        created = []
        for number in create:
            if number in garages:
                continue
            gr = Garage(number=number, area_sqm=DEFAULT_AREA, coefficient=Decimal("1"))
            s.add(gr)
            s.flush()
            s.add(PersonalAccount(garage_id=gr.id, account_number=electricity_account_number(gr.id)))
            garages[number] = gr
            created.append(number)

        added, skipped, missing = 0, [], []
        for number, files in sorted(plan.items(), key=lambda kv: (len(kv[0]), kv[0])):
            gr = garages.get(number)
            if gr is None:
                missing.append(number)
                continue
            if s.query(GaragePhoto).filter_by(garage_id=gr.id).first():
                skipped.append(number)
                continue
            for fn, caption in files:
                stored = f"{uuid.uuid4().hex}{os.path.splitext(fn)[1].lower()}"
                if not dry:
                    dst = os.path.join(upload_folder, stored)
                    shutil.copyfile(os.path.join(photos_dir, fn), dst)
                    copied.append(dst)
                    shrink_image(dst)
                s.add(GaragePhoto(garage_id=gr.id, file_path=stored, caption=caption))
                added += 1

        g.user = s.query(User).order_by(User.id).first()
        audit.record(
            "import.garage_photos",
            f"Фото гаражей с обхода территории: {added} фото к {len(plan) - len(skipped) - len(missing)} гаражам"
            + (f"; заведено гаражей: {len(created)}" if created else ""),
        )
        try:
            if dry:
                s.rollback()
            else:
                s.commit()
        except Exception:
            for p in copied:
                os.remove(p)
            raise

    print(f"Заведено гаражей: {len(created)}" + (f" ({', '.join(created)})" if created else ""))
    print(f"Фото: {added} к {len(plan) - len(skipped) - len(missing)} гаражам")
    if skipped:
        print(f"Пропущены (фото уже есть): {', '.join(skipped)}")
    if missing:
        print(f"Нет в БД (не заведены): {', '.join(missing)}")
    print("DRY-RUN — изменения отменены." if dry else "Записано.")


if __name__ == "__main__":
    main()
