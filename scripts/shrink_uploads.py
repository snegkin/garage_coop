#!/usr/bin/env python3
"""
Разовое ужатие уже загруженных фотографий в UPLOAD_FOLDER по тем же
правилам, что и при загрузке (app/images.py: до MAX_SIDE px по длинной
стороне, пережатие, без EXIF). Новые загрузки ужимаются сами. Скрипт
нужен для файлов, загруженных раньше.

Имена файлов не меняются, БД не трогается. Оригиналы остаются в уже
сделанных бэкапах (instance/backups/ их не содержит, но в restic-снимках
внешнего и локального бэкапа они есть, пока снимки не уйдут по политике
хранения).

    scripts/shrink_uploads.sh --dry-run   что будет ужато и сколько места освободится
    scripts/shrink_uploads.sh             ужать

Через .sh — с .env (UPLOAD_FOLDER/INSTANCE_DIR) и логом
instance/logs/shrink_uploads.log. Итог печатается и в консоль.
"""
import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _paths

from _paths import INSTANCE_DIR  # noqa: E402
from app.images import SHRINK_EXT, shrink_image  # noqa: E402


def upload_folder() -> str:
    # Как в config.py.
    return os.environ.get("UPLOAD_FOLDER") or os.path.join(INSTANCE_DIR, "uploads")


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} МБ"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, файлы не менять")
    parser.add_argument("--folder", default=None, help="каталог (по умолчанию UPLOAD_FOLDER)")
    args = parser.parse_args(argv)

    folder = args.folder or upload_folder()
    if not os.path.isdir(folder):
        print(f"нет каталога {folder}", file=sys.stderr)
        return 1

    total_before = total_after = changed = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in SHRINK_EXT:
                continue
            target = path
            if args.dry_run:
                target = os.path.join(tmpdir, name)
                shutil.copy2(path, target)
            result = shrink_image(target)
            if args.dry_run and os.path.exists(target):
                os.remove(target)
            if result is None:
                continue
            before, after = result
            changed += 1
            total_before += before
            total_after += after
            print(f"{name}: {_mb(before)} → {_mb(after)}")

    verb = "будет ужато" if args.dry_run else "ужато"
    print(f"Итого {verb} файлов: {changed}, {_mb(total_before)} → {_mb(total_after)} "
          f"(освобождается {_mb(total_before - total_after)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
