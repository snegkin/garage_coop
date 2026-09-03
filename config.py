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

    # Каталог клиентских mTLS-сертификатов для API банка (см.
    # app/bank_api/sberbank.py) — НЕ раздаётся ни одним роутом на скачивание
    # (в отличие от UPLOAD_FOLDER, где лежат документы кооператива и есть
    # /documents/<id>/download): приватный ключ клиентского сертификата не
    # должен быть доступен по HTTP ни при каких правах пользователя.
    BANK_CERTS_FOLDER = os.environ.get("BANK_CERTS_FOLDER") or os.path.join(BASE_DIR, "instance", "bank_certs")

    # Путь к файлу с доверенными корневыми сертификатами (CA bundle) для
    # проверки TLS-сертификата сервера банка. Сайты и API Сбербанка
    # используют сертификаты, выпущенные удостоверяющими центрами Сбера И
    # Национальным удостоверяющим центром Минцифры России (подтверждено
    # официальной документацией: developers.sber.ru/docs/ru/sber-api/start/tls)
    # — а не общемировым центром сертификации, поэтому обычный доверенный
    # набор корневых сертификатов (используемый по умолчанию Python/requests)
    # их не знает, и TLS-соединение с банком не установится (SSLError:
    # "self-signed certificate in certificate chain"), пока сюда не указан
    # бандл с обеими цепочками. Проще всего скачать готовый архив с самим
    # банком (надёжнее, чем собирать вручную с госуслуг):
    #   тестовый контур: https://cdn-app.sberdevices.ru/misc/0.0.0/assets/bsm-docs/b89853b1_chain_test.zip
    #   промышленный:    https://cdn-app.sberdevices.ru/misc/0.0.0/assets/bsm-docs/f8dd5e00_chain_prom.zip
    # Распаковать, объединить .cer/.pem файлы из архива в один (или добавить
    # к обычному системному CA-бандлу — `cat /etc/ssl/certs/ca-certificates.crt
    # chain1.cer chain2.cer > bundle.pem`, если хотите сохранить доступ и к
    # обычным HTTPS-сайтам через тот же bundle) и указать путь к получившемуся
    # файлу здесь. Без этой переменной обращения к API Сбербанка не будут
    # работать вообще, даже если токены и клиентский сертификат настроены верно.
    SBERBANK_API_CA_BUNDLE = os.environ.get("SBERBANK_API_CA_BUNDLE")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'coop.db')}"
    )
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")

    # Превью-кадры с камер видеонаблюдения (см. app/surveillance.py,
    # scripts/dvr_snapshot.py) — instance/dvr/<recorder_id>/snapshots/
    # camera_<camera_id>.jpg, пишутся cron-скриптом раз в минуту. Отдаются
    # приложением через отдельный роут (surveillance.snapshot), не как
    # статика — тот же принцип, что и у UPLOAD_FOLDER.
    DVR_SNAPSHOT_FOLDER = os.environ.get("DVR_SNAPSHOT_FOLDER") or os.path.join(BASE_DIR, "instance", "dvr")

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
