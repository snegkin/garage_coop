"""
Ограничитель частоты запросов — в первую очередь против перебора пароля на
/auth/login (сейчас там единственное ограничение — общий CSRF, брутфорс
ничем не сдерживался). Отдельный модуль, а не прямо в app/__init__.py,
чтобы auth.py мог импортировать limiter для декоратора без циклического
импорта (app/__init__.py и так импортирует auth).

Хранилище лимитов — memory:// по умолчанию (годится для одного процесса).
Если/когда деплой перейдёт на несколько воркеров gunicorn (-w > 1), лимиты
не будут общими между процессами — тогда нужно указать RATELIMIT_STORAGE_URI
(например redis://...) через переменную окружения.
"""
import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=[],  # лимиты навешиваются точечно на конкретные роуты, не глобально
)
