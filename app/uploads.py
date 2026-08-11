"""Общая функция сохранения загруженного файла на диск (без привязки к модели)."""
import os
import uuid


def save_upload(file_storage, upload_folder: str, allowed_ext: set[str] | None = None) -> str | None:
    """
    Сохраняет загруженный файл под случайным именем в upload_folder.
    Возвращает сохранённое имя файла (для записи в БД) или None, если файла
    не было / расширение не разрешено.
    """
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if allowed_ext is not None and ext not in allowed_ext:
        return None
    stored_name = f"{uuid.uuid4().hex}{ext}"
    file_storage.save(os.path.join(upload_folder, stored_name))
    return stored_name
