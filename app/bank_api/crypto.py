"""
Шифрование секретов API банка (client_secret) для хранения в БД.

Это именно шифрование (обратимое), а не хэширование пароля — секрет нужно
расшифровать обратно, чтобы подставить в запрос к банку при каждой
синхронизации. Используется Fernet (симметричный AES-128-CBC + HMAC,
из пакета `cryptography`) с ключом, производным от SECRET_KEY приложения.

Отдельная переменная окружения BANK_API_ENCRYPTION_KEY (см. .env.example)
предпочтительнее: смена SECRET_KEY (например, при компрометации сессий)
тогда не делает нечитаемыми уже сохранённые секреты банка. Если она не
задана — ключ выводится из SECRET_KEY (безопасно как временное решение
для локальной разработки, т.к. Config уже требует непустой SECRET_KEY на
проде, см. config.py и предупреждение там же).
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet() -> Fernet:
    raw_key = current_app.config.get("BANK_API_ENCRYPTION_KEY") or current_app.config["SECRET_KEY"]
    # Fernet требует ключ ровно 32 байта в base64 — SECRET_KEY/BANK_API_ENCRYPTION_KEY
    # произвольной длины, поэтому сжимаем sha256 и кодируем как urlsafe base64.
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    """Пустая строка не шифруется — храним как есть, чтобы не путать с «не задано»."""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    """Возвращает None, если token пуст или расшифровать не удалось (например,
    сменился ключ шифрования) — вызывающий код должен воспринимать это как
    «секрет недоступен, нужно ввести заново», а не падать."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
