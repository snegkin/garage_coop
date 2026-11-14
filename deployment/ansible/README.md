# Сервер с нуля (Ansible)

Плейбук `site.yml` поднимает сайт кооператива на чистом VPS: Ubuntu 24.04 или Debian 13 (Debian 12 не подходит: Python 3.11),
gunicorn за nginx, HTTPS от Let's Encrypt, cron, файрвол. БД и файлы
(`instance/uploads`, `bank_certs`, `bundles`, `dvr`) переносятся с вашего
компьютера. Дальнейшие обновления кода делает
[`deployment/deploy.sh`](../deploy/README.md): с заглушкой
«Ведутся технические работы», бэкапом и проверкой. Плейбук код только
клонирует и повторно не обновляет.

Нужно на своём компьютере: `ansible-core` ≥ 2.16 с коллекциями
`ansible.posix` и `community.general`, `rsync`; `sshpass`, если входить
на сервер по паролю.

## Настройки — вне git

```bash
mkdir -p ~/.config/garage-coop && chmod 700 ~/.config/garage-coop
cp deployment/ansible/inventory.example.ini ~/.config/garage-coop/inventory.ini
cp deployment/ansible/vars.example.yml      ~/.config/garage-coop/vars.yml
chmod 600 ~/.config/garage-coop/*
```

- `inventory.ini` — IP нового сервера, вход под `root`.
- `vars.yml` — домен, e-mail для Let's Encrypt, ключи и пути к локальной
  БД. Остальное, со значениями по умолчанию, лежит в `defaults.yml`.
  `COOP_VARS=/путь` позволяет взять другой файл.

Можно держать оба файла и прямо в `deployment/ansible/` (они в
`.gitignore`), тогда пути указываются явно:

```bash
cd deployment/ansible
COOP_VARS=vars.yml ansible-playbook -i inventory.ini site.yml
```

**Ключи шифрования.** `BANK_API_ENCRYPTION_KEY` должен быть тем же, что
на старом сервере, иначе все сохранённые в БД пароли и токены придётся
вводить заново. В `vars.example.yml` ключи по умолчанию читаются из
продового `.env` на этом компьютере.

**Несколько доменов.** Если у кооператива есть ещё домены (например,
основной в зоне .рф и такой же в .ru), перечислите их в
`coop_domain_aliases` (тоже в punycode). Они попадут в тот же сертификат
(`--cert-name coop_domain --expand`) и будут отвечать постоянным
редиректом (301) на `coop_domain`, потому что вход, reCAPTCHA и ссылки
в письмах привязаны к одному домену. A-записи всех доменов должны
указывать на этот сервер. Если DNS дополнительного домена ещё не
обновился, основной всё равно получит HTTPS, а дополнительный пока
работает только по HTTP; следующий прогон допишет его в сертификат. У
дополнительного кооператива для этого есть поле `aliases`.

## Переезд

1. **Код с миграциями — в GitHub.** Сервер клонирует `origin`, а не
   локальную копию. Если локальная БД на ревизии схемы, которой нет в
   `origin/<ветка>`, плейбук остановится до замены БД с подсказкой.
2. **DNS.** A-запись домена направьте на новый IP, лучше заранее: без
   неё certbot не выдаст сертификат. Плейбук тогда не падает: сайт
   работает по HTTP, а после обновления DNS прогоните его ещё раз.
3. **Прогон:**

   ```bash
   cd deployment/ansible
   ansible-playbook site.yml --check --diff   # что изменится (на чистом сервере — частично)
   ansible-playbook site.yml
   ```

   Или `deployment/setup.sh` (аргументы те же, что у `ansible-playbook`): запущенный
   двойным щелчком из файлового менеджера, он сам откроет окно терминала,
   а после прогона подождёт Enter. Без `~/.config/garage-coop/vars.yml` и
   `inventory.ini` он берёт одноимённые файлы рядом с собой.

   При первом прогоне сервер создаёт себе deploy key, плейбук печатает его
   и ждёт. Добавьте ключ в GitHub → Settings → Deploy keys (только чтение)
   и нажмите Enter.
4. **Проверка:** вход, баланс банка, почта, кадры камер. Затем по списку
   в [docs/deployment.md](../../docs/deployment.md#деплой-в-проде).

Что делает плейбук:

| Шаг | Теги |
|---|---|
| Часовой пояс, пакеты (+ шрифты Liberation/DejaVu для PDF), автообновления безопасности, swap, ufw (SSH, 80, 443), fail2ban | `system` |
| Пользователь `coop`, ключи для `deploy.sh`, sudo без пароля **только** на `systemctl restart garage-coop`, deploy key, клон, venv, gunicorn | `app` |
| Дополнительные кооперативы из `coop_extra_instances` (раздел ниже) | `instances`, `app` |
| Снимок локальной БД через backup API (можно при запущенном локальном сайте), проверки целостности и ревизии схемы, заливка; rsync каталогов `instance/` | `restore` |
| nginx (статика, заглушка, прокси), certbot `--webroot`, HTTPS + HSTS после получения сертификата, reload при продлении | `nginx` |
| `.env`, systemd-юнит (`repair_db.sh` перед стартом), проверка ответа, cron; при `backup_agent_pubkeys` — первый запуск `backup_offsite`, если `instance/backup_staging/` ещё нет: агент председателя сразу находит, что забрать | `app` |
| Конфиг `deploy.sh` (`deploy_conf_path`: по умолчанию `~/.config/garage-coop/deploy.conf`, можно `deployment/deploy/deploy.conf`), если его ещё нет | `app` |
| **На этом компьютере**, если `local_backup_agent: true`: агент бэкапа целиком. Ключ агента сам попадает на сервер, `known_hosts` заполняется ключом сервера из его `/etc/ssh` (без `ssh-keyscan`), пароль restic создаётся один раз. Затем первый проход и таймер | `local` |
| **На этом компьютере**, если `local_ezp_agent: true`: КриптоПро CSP из `agents/ezp/cryptopro/*.deb`, цепочка ФНС в `mRoot`/`mCA`, сертификат КЭП с Рутокена в хранилище `ezp-agent` (токен должен быть вставлен), конфиги, службы и `check`. Пока чего-то не хватает, службы не запускаются, а плейбук пишет, чего именно | `local` |

Агенты ставятся вторым play на `localhost` через sudo, поэтому нужен
пароль sudo. `deployment/setup.sh` спрашивает его сам (по-русски, сразу проверяет и
передаёт в ansible через `--become-password-file`), когда в `vars.yml`
включён один из агентов; напрямую — `ansible-playbook … -K`. Только агенты: `deployment/setup.sh --tags local`. Письмо в Почту
и тестовые письма (`agents/ezp/README.md`, шаги 6 и «Проверка») остаются
ручными.

## Несколько кооперативов на одном сервере

Основной кооператив — как выше. Дополнительные описываются списком
`coop_extra_instances` в `vars.yml` (все поля — в `defaults.yml`):

```yaml
coop_extra_instances:
  - name: coop2                    # пользователь ОС и служба garage-coop-coop2
    domain: xn--...xn--p1ai        # punycode, A-запись — на этот сервер
    port: 8001
    secret_key: "…"                # свои, не как у основного
    bank_api_encryption_key: "…"
```

Код и venv у всех общие (`app_dir` основного), обновляются одним
`deploy.sh`. Для каждого дополнительного кооператива плейбук делает:

- пользователь `NAME`;
- `/home/NAME/coop.env` (0600) с `INSTANCE_DIR=/home/NAME/instance` (0700);
- новая БД (`seed.py`) или перенос `restore_db_file`;
- служба `garage-coop-NAME` (`repair_db.sh` с `ENV_FILE`) на своём порту;
- vhost и сертификат (`default_server` остаётся только у основного);
- crontab с `ENV_FILE` и общей блокировкой `COOP_CRON_LOCK` (её получает
  и основной), минуты сдвинуты на `cron_minute_offset`;
- правило sudo для `deploy.sh`: перезапуск этой службы и бэкап её БД под
  её пользователем;
- ключи агента бэкапа председателя этого кооператива
  (`backup_agent_pubkeys`, с `ENV_FILE` в `command=`).

Плейбук проверяет, что пользователь `NAME` видит общий код, но не `.env` и
`instance/` основного. Тогда забытый `ENV_FILE` даёт отказ «нет доступа», а
не запись в чужую БД. Только дополнительные кооперативы:
`deployment/setup.sh --tags instances`.

`deploy.conf` создаётся один раз. Если кооператив добавлен позже, плейбук
предупредит, каких служб в нём нет, и покажет строки `SERVICES`,
`HEALTH_URLS`, `BACKUP_ENV_FILES`, которые нужно вписать.

Агенты на компьютере председателя (бэкап, ЭЗП) — по одному на компьютер,
то есть для своего кооператива. Председатель другого кооператива ставит
их на своём компьютере по `agents/*/README.md`, указав в конфиге агента
`user = NAME`. Удалять кооператив плейбук не умеет: службу, юнит, vhost и
crontab удаляют руками, данные остаются.

## Повторные прогоны

Повторный прогон безопасен: изменения применяются только там, где
состояние разошлось с описанным.

- БД на сервере уже есть, поэтому не трогается. `-e restore_force=true`
  перезапишет её локальной, а прежнюю сохранит в
  `instance/backups/pre_restore_*.db`.
- Файлы переносятся один раз (метка `instance/.restored-files`) и без
  `--delete`.
- `.env` генерируется из `vars.yml`: правка руками на сервере пропадёт при
  следующем прогоне.
- Включить cron-задачу (eWeLink, камеры, Telegram): в `vars.yml`
  `cron_enable: [dvr_snapshot, poll_ewelink]` (выключить —
  `cron_disable: [...]`), затем `ansible-playbook site.yml --tags system,app`.
  С `dvr_snapshot` сам ставится `ffmpeg`.
