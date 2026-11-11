"""Ужатие загруженных фотографий.

Снимок с телефона — 4000×3000 и 5–8 МБ, а на сайте он нигде не
показывается крупнее экрана. Такие файлы раздувают uploads/, а за ним
бэкапы: локальный (backup_agent/), внешний (scripts/backup_offsite.py) и
instance/backups/. Поэтому фото при загрузке уменьшается до MAX_SIDE по
длинной стороне и пережимается. Имя и расширение файла не меняются, так
что ссылки в БД остаются прежними. Уже загруженные файлы ужимает
scripts/shrink_uploads.py, по той же функции.

Что не трогаем:
- gif и анимированный webp (кадры потерялись бы);
- файлы, которые и так невелики;
- файлы, которые Pillow не открыл (битые, огромные): лучше оригинал,
  чем ошибка загрузки.

EXIF при пережатии не сохраняется. Поворот из EXIF (Orientation)
применяется к пикселям заранее, остальное (GPS, модель телефона) сайту
не нужно и только раскрывает, где снимали. ICC-профиль сохраняется.
"""
import logging
import os

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

MAX_SIDE = 2000          # px по длинной стороне
JPEG_QUALITY = 85
WEBP_QUALITY = 85
# Меньше этого и не больше MAX_SIDE по стороне — оставляем как есть:
# пережатие небольшого файла почти ничего не даёт, а качество теряется.
MIN_BYTES = 1024 * 1024

SHRINK_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def shrink_image(path: str) -> tuple[int, int] | None:
    """Ужимает фото на месте. Возвращает (было, стало) в байтах, если файл
    заменён, иначе None."""
    if os.path.splitext(path)[1].lower() not in SHRINK_EXT:
        return None
    tmp = f"{path}.shrink.tmp"
    try:
        before = os.path.getsize(path)
        with Image.open(path) as img:
            fmt = img.format
            if fmt not in ("JPEG", "PNG", "WEBP") or getattr(img, "is_animated", False):
                return None
            if max(img.size) <= MAX_SIDE and before <= MIN_BYTES:
                return None
            icc = img.info.get("icc_profile")
            out = ImageOps.exif_transpose(img)
            out.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            params = {"icc_profile": icc} if icc else {}
            if fmt == "JPEG":
                if out.mode not in ("RGB", "L"):
                    out = out.convert("RGB")
                out.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True, **params)
            elif fmt == "WEBP":
                out.save(tmp, "WEBP", quality=WEBP_QUALITY, method=6, **params)
            else:
                out.save(tmp, "PNG", optimize=True, **params)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        log.warning("фото %s не ужато: %s", path, exc)
        if os.path.exists(tmp):
            os.remove(tmp)
        return None

    after = os.path.getsize(tmp)
    if after >= before:
        os.remove(tmp)
        return None
    os.replace(tmp, path)
    return before, after
