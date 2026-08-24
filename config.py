import os
import warnings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_INSECURE_DEFAULT_SECRET = "измени-меня-в-проде"


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", _INSECURE_DEFAULT_SECRET)
    if SECRET_KEY == _INSECURE_DEFAULT_SECRET:
        # Не роняем локальную разработку/тесты, но громко предупреждаем —
        # с дефолтным ключом любой может подделать сессию (в т.ч. session["user_id"]
        # председателя). В проде обязательно задать переменную окружения SECRET_KEY.
        warnings.warn(
            "SECRET_KEY не задан переменной окружения — используется небезопасное "
            "значение по умолчанию. Это допустимо только для локальной разработки. "
            "Перед деплоем в прод обязательно установите переменную окружения SECRET_KEY "
            "(например: python -c \"import secrets; print(secrets.token_hex(32))\").",
            RuntimeWarning,
            stacklevel=2,
        )

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'coop.db')}"
    )
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")

    # Ограничение размера входящего запроса (загрузка файлов и т.д.) —
    # без этого анонимный/залогиненный пользователь может положить сервер
    # запросами с гигантскими телами. 25 МБ с запасом покрывает фото/сканы/протоколы.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 25 * 1024 * 1024))

    # Cookie сессии. SESSION_COOKIE_SECURE выключен по умолчанию, т.к. локальная
    # разработка обычно идёт по http://localhost — включайте FORCE_HTTPS=1 в проде
    # (за реверс-прокси с HTTPS).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("FORCE_HTTPS", "0") == "1"

    # Flask-WTF CSRF: токен живёт столько же, сколько разумно ожидать, что
    # пользователь не закроет открытую форму (например, длинную форму импорта CSV).
    WTF_CSRF_TIME_LIMIT = None
