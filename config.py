import os
import warnings
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Каталог данных экземпляра сайта (БД, загрузки, кадры камер, сертификаты
# банка, логи/блокировки cron-скриптов — см. scripts/_common.sh). Отдельная
# переменная — чтобы с одного checkout'а кода можно было запускать несколько
# кооперативов, у каждого свой INSTANCE_DIR в своём .env (docs/deployment.md,
# «Несколько кооперативов на одном сервере»).
INSTANCE_DIR = os.environ.get("INSTANCE_DIR") or os.path.join(BASE_DIR, "instance")

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

    # Ключ шифрования секретов API банка (client_secret и т.п. в BankApiCredential,
    # см. app/bank_api/crypto.py). Если не задан — используется SECRET_KEY, что
    # достаточно для локальной разработки, но означает, что ротация SECRET_KEY
    # на проде сделает нечитаемыми уже сохранённые секреты банка — на проде,
    # где включена интеграция с API банка, лучше задать отдельно.
    BANK_API_ENCRYPTION_KEY = os.environ.get("BANK_API_ENCRYPTION_KEY")
    # Адрес API СберБизнес, если отличается от значений по умолчанию в
    # app/bank_api/sberbank.py (SANDBOX_BASE_URL/PROD_BASE_URL) — тестовый
    # контур подтверждён напрямую в личном кабинете Sber API
    # (fintech-test.sberbank.ru:9443), промышленный — по документации, не
    # проверен живым запросом.
    SBERBANK_API_BASE_URL = os.environ.get("SBERBANK_API_BASE_URL")
    # Эндпоинт обновления access_token через refresh_token, если отличается
    # от значения по умолчанию (<SBERBANK_API_BASE_URL>/ic/sso/api/v2/oauth/token,
    # см. app/bank_api/sberbank.py) — путь подтверждён документацией, хост
    # выводится из базового адреса API, не отдельно задокументирован для
    # каждого контура.
    SBERBANK_API_TOKEN_URL = os.environ.get("SBERBANK_API_TOKEN_URL")
    # Адрес API Модульбанка, если отличается от https://api.modulbank.ru/v1
    # (app/bank_api/modulbank.py: DEFAULT_BASE_URL). Песочница — тот же адрес
    # с заголовком sandbox: on, отдельного хоста нет.
    MODULBANK_API_BASE_URL = os.environ.get("MODULBANK_API_BASE_URL")

    # Каталог клиентских mTLS-сертификатов для API банка (см.
    # app/bank_api/sberbank.py) — НЕ раздаётся ни одним роутом на скачивание
    # (в отличие от UPLOAD_FOLDER, где лежат документы кооператива и есть
    # /documents/<id>/download): приватный ключ клиентского сертификата не
    # должен быть доступен по HTTP ни при каких правах пользователя.
    BANK_CERTS_FOLDER = os.environ.get("BANK_CERTS_FOLDER") or os.path.join(INSTANCE_DIR, "bank_certs")

    # Доверенные корневые сертификаты УЦ для проверки TLS-сертификатов
    # серверов банков: оба банка используют российские УЦ, которых нет в
    # certifi (requests по умолчанию) — без них SSLError "self-signed
    # certificate in certificate chain". В git файлов нет: deployment/deploy.sh
    # кладёт их сюда из локального deployment/deploy/bundles/
    # (deployment/deploy/README.md, «Сертификаты»). Отдельно от
    # BANK_CERTS_FOLDER: там приватные ключи mTLS счетов, загруженные через
    # интерфейс, а здесь — публичные корни, которые приносит деплой.
    BANK_CA_BUNDLES_FOLDER = os.environ.get("BANK_CA_BUNDLES_FOLDER") or os.path.join(INSTANCE_DIR, "bundles")
    # Сбербанк: fintech(-test).sberbank.ru:9443 (на ноябрь 2026) отдают цепочку
    # SberCA Ext → SberCA Root Ext. Документация банка
    # (developers.sber.ru/docs/ru/sber-api/start/tls) велит доверять УЦ Сбера
    # и Минцифры — bundle.pem собирается из архива банка:
    #   тестовый контур: https://cdn-app.sberdevices.ru/misc/0.0.0/assets/bsm-docs/b89853b1_chain_test.zip
    #   промышленный:    https://cdn-app.sberdevices.ru/misc/0.0.0/assets/bsm-docs/f8dd5e00_chain_prom.zip
    # (распаковать и `cat *.cer > bundle.pem`).
    SBERBANK_API_CA_BUNDLE = os.environ.get("SBERBANK_API_CA_BUNDLE") or os.path.join(
        BANK_CA_BUNDLES_FOLDER, "bundle.pem")
    # Модульбанк: api.modulbank.ru — Russian Trusted Sub CA → Russian
    # Trusted Root CA (НУЦ Минцифры, корень действует до 2032 года).
    MODULBANK_API_CA_BUNDLE = os.environ.get("MODULBANK_API_CA_BUNDLE") or os.path.join(
        BANK_CA_BUNDLES_FOLDER, "russian_trusted_root_ca.pem")

    # Google reCAPTCHA v2 (чекбокс «Я не робот») на странице восстановления
    # пароля (см. app/recaptcha.py, app/auth.py: forgot_password) — ключи
    # выпускаются на конкретный домен в консоли Google, поэтому это
    # деплой-секрет (как SECRET_KEY), а не бизнес-данные кооператива:
    # переменные окружения, не форма в админке. Если не заданы —
    # app/recaptcha.py:verify() пропускает проверку (не блокирует
    # локальную разработку и тесты) — но предупреждаем в логе при старте,
    # тем же приёмом, что и у SECRET_KEY без переменной окружения, чтобы
    # отсутствие защиты на проде не было тихим.
    RECAPTCHA_SITE_KEY = os.environ.get("RECAPTCHA_SITE_KEY")
    RECAPTCHA_SECRET_KEY = os.environ.get("RECAPTCHA_SECRET_KEY")
    if not (RECAPTCHA_SITE_KEY and RECAPTCHA_SECRET_KEY):
        warnings.warn(
            "RECAPTCHA_SITE_KEY/RECAPTCHA_SECRET_KEY не заданы переменными окружения — "
            "страница восстановления пароля работает БЕЗ защиты от автоматических запросов. "
            "Это допустимо только для локальной разработки. Ключи можно получить на "
            "https://www.google.com/recaptcha/admin (тип reCAPTCHA v2 «Я не робот»).",
            RuntimeWarning,
            stacklevel=2,
        )

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(INSTANCE_DIR, 'coop.db')}"
    )
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER") or os.path.join(INSTANCE_DIR, "uploads")

    # Каталог логов — тот же, куда пишут cron-обёртки (scripts/_common.sh:
    # $INSTANCE_DIR/logs); смотреть их можно на странице /system-logs/.
    LOG_FOLDER = os.path.join(INSTANCE_DIR, "logs")

    # Отладочный журнал обмена с внешними API (банк, eWeLink, СМС, Telegram…)
    # — см. app/api_log.py. Файл на день в LOG_FOLDER, секреты маскируются.
    # API_LOG_ENABLED=0 — выключить.
    API_LOG_ENABLED = os.environ.get("API_LOG_ENABLED", "1") not in ("0", "false", "False", "")
    API_LOG_KEEP_DAYS = int(os.environ.get("API_LOG_KEEP_DAYS", "7"))
    API_LOG_MAX_BODY = int(os.environ.get("API_LOG_MAX_BODY", "200000"))

    # Превью-кадры с камер видеонаблюдения (см. app/surveillance.py,
    # scripts/dvr_snapshot.py) — instance/dvr/<recorder_id>/snapshots/
    # camera_<camera_id>.jpg, пишутся cron-скриптом раз в минуту. Отдаются
    # приложением через отдельный роут (surveillance.snapshot), не как
    # статика — тот же принцип, что и у UPLOAD_FOLDER.
    DVR_SNAPSHOT_FOLDER = os.environ.get("DVR_SNAPSHOT_FOLDER") or os.path.join(INSTANCE_DIR, "dvr")

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

    # Приложение за реверс-прокси (nginx, см. INSTALL.md): доверять его
    # заголовкам X-Forwarded-For/-Proto/-Host (werkzeug ProxyFix, один
    # прокси). Без этого за nginx каждый посетитель для приложения —
    # 127.0.0.1: лимит попыток входа (rate_limit.py) становится общим на
    # всех, reCAPTCHA получает не тот адрес, ссылки в уведомлениях — http://.
    # Без прокси НЕ включать: заголовки подделал бы любой посетитель.
    BEHIND_PROXY = os.environ.get("BEHIND_PROXY", "0") == "1"

    # Срок жизни сессии при отмеченной галке "Запомнить меня" (см.
    # auth._complete_login: session.permanent=True включает именно этот
    # срок). Без галки сессия остаётся обычной cookie сессии браузера —
    # стирается при его закрытии, PERMANENT_SESSION_LIFETIME на неё не
    # влияет (Flask применяет это значение только когда session.permanent
    # выставлен в True).
    PERMANENT_SESSION_LIFETIME = timedelta(days=365)

    # Flask-WTF CSRF: токен живёт столько же, сколько разумно ожидать, что
    # пользователь не закроет открытую форму (например, длинную форму импорта CSV).
    WTF_CSRF_TIME_LIMIT = None
