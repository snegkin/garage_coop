# Установка на сервер

Пошаговая установка одного кооператива на сервер с Linux (команды — для
Debian/Ubuntu): gunicorn за nginx с HTTPS. Локальный запуск для разработки —
в [README](README.md#быстрый-старт); подробности по каждой настройке, все
cron-скрипты, резервное копирование и несколько кооперативов на одном
сервере — в [docs/deployment.md](docs/deployment.md).

Ниже `coop` — пользователь ОС, под которым работает сайт, `/home/coop/garage_coop` —
каталог с кодом, `coop.example.ru` — домен. Подставьте свои.

## 1. Системные пакеты

```bash
sudo apt install git python3 python3-venv sqlite3 nginx certbot python3-certbot-nginx
# по необходимости:
sudo apt install ffmpeg                                # кадры с камер видеонаблюдения
sudo apt install libpango-1.0-0 libpangoft2-1.0-0      # PDF (weasyprint): ПД-4, документы, заказные письма
```

Нужен Python 3.12 или новее (на нём гоняются тесты в CI). Node.js и сборка
фронтенда не нужны: библиотеки (Bootstrap, иконки, графики) лежат в
репозитории готовыми файлами, см. [app/static/vendor/README.md](app/static/vendor/README.md).

## 2. Код и зависимости

```bash
sudo adduser --disabled-password coop
sudo -iu coop
git clone <адрес репозитория> garage_coop && cd garage_coop
python3 -m venv venv
venv/bin/pip install -r requirements.txt gunicorn
```

## 3. Настройки (.env)

```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # дважды: для SECRET_KEY и BANK_API_ENCRYPTION_KEY
```

В `.env` как минимум:

```bash
SECRET_KEY=...                # сгенерированный
BANK_API_ENCRYPTION_KEY=...   # сгенерированный, другой; потеряете — заново вводить пароли/токены банка, почты, Telegram
FORCE_HTTPS=1                 # cookie только по HTTPS
BEHIND_PROXY=1                # приложение за nginx — доверять его X-Forwarded-*
FLASK_DEBUG=0
```

`BEHIND_PROXY=1` обязателен за nginx: без него приложение видит всех
посетителей как 127.0.0.1 — лимит попыток входа становится общим на всех
(один перебирающий пароли заблокирует вход остальным), а ссылки в
уведомлениях получаются `http://`. И наоборот — без прокси его не включать.

Остальные переменные (reCAPTCHA, `INSTANCE_DIR`, лимиты) — в комментариях
`.env.example`. Приложение не читает `.env` само: веб-процессу его передаёт
systemd (шаг 5), cron-обёртки `scripts/*.sh` подгружают его сами.

## 4. База данных

```bash
set -a; . ./.env; set +a
venv/bin/python seed.py      # БД в instance/coop.db, пользователь chairman / change-me-please
```

Миграции схемы дальше накатываются сами при каждом старте приложения.

## 5. Служба systemd

`/etc/systemd/system/garage-coop.service`:

```ini
[Unit]
Description=Garage cooperative
After=network.target

[Service]
User=coop
WorkingDirectory=/home/coop/garage_coop
EnvironmentFile=/home/coop/garage_coop/.env
ExecStartPre=/home/coop/garage_coop/scripts/repair_db.sh
ExecStart=/home/coop/garage_coop/venv/bin/gunicorn -w 1 --threads 4 -k gthread -b 127.0.0.1:8000 run:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now garage-coop
```

Один процесс с потоками — под нагрузку кооператива этого хватает и на
сервере с 1 ГБ ОЗУ; почему не несколько воркеров и что тогда делать с
лимитами входа — [docs/deployment.md](docs/deployment.md#деплой-в-проде).
`repair_db.sh` перед стартом проверяет целостность SQLite (там же).

## 6. nginx

`/etc/nginx/sites-available/garage-coop` (затем
`sudo ln -s ../sites-available/garage-coop /etc/nginx/sites-enabled/`):

```nginx
server {
    listen 80;
    server_name coop.example.ru;

    # Тот же предел, что MAX_CONTENT_LENGTH приложения (по умолчанию 25 МБ),
    # с запасом на поля формы — иначе nginx отвергнет скан раньше приложения.
    client_max_body_size 30m;

    # Статика — напрямую с диска, мимо приложения.
    location /static/ {
        alias /home/coop/garage_coop/app/static/;
    }
    # Сторонние библиотеки: путь содержит версию (<пакет>-<версия>/),
    # файл по нему никогда не меняется — кэш браузера на год.
    location /static/vendor/ {
        alias /home/coop/garage_coop/app/static/vendor/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        # Загрузка выписки из банка, формирование PDF — бывает дольше минуты.
        proxy_read_timeout 120s;
    }

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
}
```

- Загруженные документы, фото гаражей, кадры камер (`instance/`) через
  `location` **не** публикуются: их отдаёт само приложение с проверкой прав.
- nginx (пользователь `www-data`) должен иметь право читать
  `app/static/`: например, `chmod o+x /home/coop /home/coop/garage_coop /home/coop/garage_coop/app`.
- gzip заметно ужимает библиотеки (Mermaid — 3,5 МБ → около 1 МБ).

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d coop.example.ru     # HTTPS и редирект с http — certbot допишет в этот же файл
```

## 7. Первый вход и cron

1. Откройте `https://coop.example.ru`, войдите как `chairman` /
   `change-me-please` и **сразу смените пароль** («Мой профиль»).
2. Пройдите мастер настройки `/setup`.
3. Настройте cron (`crontab -e` пользователя `coop`): как минимум ставка ЦБ
   и резервное копирование, остальное — по используемым функциям (банк,
   СМС, eWeLink, камеры, почта, Telegram). Готовые строки —
   [docs/deployment.md](docs/deployment.md#автоматизация-cron).

Чек-лист перед реальной работой — в [docs/deployment.md](docs/deployment.md#деплой-в-проде).

## Обновление

```bash
sudo -iu coop
cd garage_coop
git pull
venv/bin/pip install -r requirements.txt
./scripts/vendor.sh verify                   # библиотеки фронтенда совпадают с манифестом (без сети)
exit
sudo systemctl restart garage-coop            # миграции БД накатятся при старте
```

Библиотеки фронтенда приходят вместе с кодом (`git pull`) — отдельно
ничего ставить не нужно. Как обновлять сами библиотеки (это делается в
репозитории, не на сервере) — [app/static/vendor/README.md](app/static/vendor/README.md).
