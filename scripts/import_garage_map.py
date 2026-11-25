#!/usr/bin/env python3
"""
Загрузка первичной раскладки схемы территории (страница «Схема территории»,
app/garage_map.py) из JSON-файла.

Запуск:
    INSTANCE_DIR=/path/instance venv/bin/python scripts/import_garage_map.py LAYOUT.json [--replace] [--dry-run]

LAYOUT.json:
    {"width": 1256, "height": 5976,
     "shapes": [
        {"kind": "road", "points": [[x, y], ...]},
        {"kind": "building", "label": "1Г", "points": [...]},
        {"kind": "garage", "number": "17а", "points": [...]},   # первое ребро — сторона ворот
     ]}

Боксы привязываются к гаражам реестра по номеру (без учёта регистра); не
найденный номер становится подписью контура без привязки — его можно
привязать потом в режиме правки. Если на схеме уже есть фигуры, скрипт
ничего не делает, пока не указан --replace (тогда схема заменяется целиком).
--dry-run — показать, что будет сделано, и откатить.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, database  # noqa: E402
from app.garage_map import get_settings  # noqa: E402
from app.models import Garage, GarageMapShape  # noqa: E402


def import_layout(layout: dict, replace: bool) -> tuple[int, list[str]]:
    session = database.db_session
    existing = session.query(GarageMapShape).count()
    if existing and not replace:
        raise SystemExit(f"На схеме уже {existing} фигур — для замены запустите с --replace.")
    session.query(GarageMapShape).delete()
    session.flush()

    settings = get_settings()
    settings.width = int(layout["width"])
    settings.height = int(layout["height"])

    by_number = {g.number.strip().lower(): g for g in session.query(Garage).all()}
    unmatched, used = [], set()
    for index, item in enumerate(layout["shapes"]):
        shape = GarageMapShape(kind=item.get("kind", "garage"), points=json.dumps(item["points"]), sort_order=index)
        if shape.kind == "garage":
            number = str(item["number"])
            garage = by_number.get(number.strip().lower())
            if garage is not None and garage.id not in used:
                shape.garage_id = garage.id
                used.add(garage.id)
            else:
                shape.label = number
                unmatched.append(number)
        else:
            shape.label = item.get("label")
        session.add(shape)
    session.flush()
    return len(layout["shapes"]), unmatched


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__.split("LAYOUT.json:")[0])
    with open(args[0], encoding="utf-8") as fh:
        layout = json.load(fh)

    app = create_app()
    with app.app_context():
        total, unmatched = import_layout(layout, replace="--replace" in sys.argv)
        placed = {s.garage_id for s in database.db_session.query(GarageMapShape).filter(GarageMapShape.garage_id.isnot(None))}
        missing = sorted(g.number for g in database.db_session.query(Garage).all() if g.id not in placed)
        if "--dry-run" in sys.argv:
            database.db_session.rollback()
        else:
            database.db_session.commit()
    print(f"Фигур: {total}")
    print("Номера без гаража в реестре (останутся подписями): " + (", ".join(unmatched) or "нет"))
    print("Гаражи реестра, которых нет на схеме: " + (", ".join(missing) or "нет"))


if __name__ == "__main__":
    main()
