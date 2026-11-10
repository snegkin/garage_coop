# Сервер с нуля (Ansible)

Плейбук `site.yml` поднимает сайт кооператива на чистом VPS: Ubuntu 24.04 или Debian 13 (Debian 12 не подходит: Python 3.11),
gunicorn за nginx, HTTPS от Let's Encrypt, cron, файрвол. БД и файлы
(`instance/uploads`, `bank_certs`, `bundles`, `dvr`) переносятся с вашего
компьютера. Дальнейшие обновления кода делает
[`deploy-from-git/deploy.sh`](../deploy-from-git/README.md): с заглушкой
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
| Снимок локальной БД через backup API (можно при запущенном локальном сайте), проверки целостности и ревизии схемы, заливка; rsync каталогов `instance/` | `restore` |
| nginx (статика, заглушка, прокси), certbot `--webroot`, HTTPS + HSTS после получения сертификата, reload при продлении | `nginx` |
| `.env`, systemd-юнит (`repair_db.sh` перед стартом), проверка ответа, cron | `app` |
| Конфиг `deploy.sh` (`deploy_conf_path`: по умолчанию `~/.config/garage-coop/deploy.conf`, можно `deployment/deploy-from-git/deploy.conf`), если его ещё нет | `app` |

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
