"""Общая функция сохранения загруженного файла на диск (без привязки к модели)."""
import os
import uuid

# Дефолтный белый список для "документных" загрузок (новости, протоколы,
# документы, акты сверки и т.п.). Раньше все вызовы save_upload() шли без
# allowed_ext вообще — можно было залить .html/.svg/.js и получить их обратно
# отданными тем же origin'ом (send_from_directory отдаёт их с исходным
# content-type/встроенным <script>) — то есть stored XSS. .svg сюда
# намеренно не входит: он может содержать <script>, для картинок есть
# отдельный ALLOWED_PHOTO_EXT в garages.py (jpg/png/webp/gif).
DEFAULT_ALLOWED_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".txt", ".csv",
}


def save_upload(file_storage, upload_folder: str, allowed_ext: set[str] | None = DEFAULT_ALLOWED_EXT) -> str | None:
    """
    Сохраняет загруженный файл под случайным именем в upload_folder.
    Возвращает сохранённое имя файла (для записи в БД) или None, если файла
    не было / расширение не разрешено.

    allowed_ext по умолчанию — DEFAULT_ALLOWED_EXT (безопасный для веба
    список). Передайте allowed_ext=None явно, только если точно нужно снять
    ограничение (не рекомендуется), либо свой набор — как для фото гаражей.
    """
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if allowed_ext is not None and ext not in allowed_ext:
        return None
    stored_name = f"{uuid.uuid4().hex}{ext}"
    file_storage.save(os.path.join(upload_folder, stored_name))
    return stored_name
