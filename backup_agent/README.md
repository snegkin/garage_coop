# Агент локального бэкапа

Программа для компьютера председателя (Linux, systemd). Раз в сутки
забирает с сервера кооператива копию данных и хранит её в локальном
restic-репозитории с историей версий.

| Копия | Где | Что защищает |
|---|---|---|
| `instance/backups/` | сам сервер (`backup_db`) | от порчи БД, случайного удаления |
| внешний restic | Яндекс.Диск (`backup_offsite`) | от смерти сервера и хостера |
| **этот агент** | компьютер председателя | от смерти хостера и облака, от взлома сервера |

Что забирается: снимок БД, файл окружения (`.env` с `SECRET_KEY` и
`BANK_API_ENCRYPTION_KEY`, без них сохранённые в БД пароли и токены не
расшифровать), `uploads/`, `bank_certs/`, `bundles/` и архив почты
`mail_backup/`. Всё это на сервере готовят cron-задачи `backup_mailbox` и
`backup_offsite`, см. [docs/deployment.md](../docs/deployment.md), «Внешний бэкап».

**Почему так:** агент сам ходит на сервер (компьютер за NAT), а у сервера
нет доступа к этой копии. Ключ агента на сервере ограничен
`scripts/backup_pull_gate.sh`: только rsync, только чтение, только `instance/`.

## Установка

1. **Пакеты:** `sudo apt install python3 rsync restic`.

2. **Пользователь и каталоги:**
   ```bash
   sudo useradd --system --home-dir /var/lib/coop-backup --shell /usr/sbin/nologin coop-backup
   sudo install -d -m 0750 -o root -g coop-backup /etc/coop-backup
   sudo install -d /opt/coop-backup
   sudo install -m 0755 coop_backup_agent.py /opt/coop-backup/
   ```

3. **ssh-ключ агента:**
   ```bash
   sudo ssh-keygen -t ed25519 -N '' -C coop-backup -f /etc/coop-backup/id_ed25519
   sudo chown root:coop-backup /etc/coop-backup/id_ed25519 && sudo chmod 0640 /etc/coop-backup/id_ed25519
   sudo cat /etc/coop-backup/id_ed25519.pub
   ```
   **На сервере** добавить строку в `~/.ssh/authorized_keys` пользователя
   сайта (`coop`). Нужен именно `command=…,restrict`, без него ключ даёт
   полный shell:
   ```
   command="/home/coop/garage_coop/scripts/backup_pull_gate.sh",restrict ssh-ed25519 AAAA… coop-backup
   ```
   Через ansible: `backup_agent_pubkeys` в `vars.yml`, плейбук добавит строку сам.

   Ключ сервера в known_hosts агента:
   ```bash
   ssh-keyscan -p 22 coop.example.ru | sudo tee /etc/coop-backup/known_hosts
   ```
   Отпечаток сверить с `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` на сервере.

4. **Пароль репозитория:**
   ```bash
   openssl rand -base64 32 | sudo tee /etc/coop-backup/restic-password
   sudo chown root:coop-backup /etc/coop-backup/restic-password && sudo chmod 0640 /etc/coop-backup/restic-password
   ```
   **Сохраните пароль ещё где-нибудь** (менеджер паролей, бумага в сейфе):
   если умрёт диск компьютера, без пароля не открыть и копию репозитория.

5. **Конфиг:**
   ```bash
   sudo install -m 0640 -o root -g coop-backup coop_backup.example.ini /etc/coop-backup/coop_backup.ini
   ```
   Вписать `host`. Репозиторий лучше держать на отдельном диске:
   `repository = /mnt/backup/coop-restic`, владелец каталога — `coop-backup`.
   Диск должен монтироваться при загрузке (fstab), иначе restic создаст
   новый пустой репозиторий в точке монтирования.

6. **Проверка и таймер:**
   ```bash
   sudo install -m 0644 coop-backup.service coop-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl start coop-backup        # первый проход — несколько минут
   journalctl -u coop-backup -n 50
   sudo systemctl enable --now coop-backup.timer
   ```

## Повседневное

```bash
systemctl list-timers coop-backup.timer   # когда следующий запуск
systemctl status coop-backup              # итог последнего
sudo -u coop-backup python3 /opt/coop-backup/coop_backup_agent.py status
sudo -u coop-backup python3 /opt/coop-backup/coop_backup_agent.py snapshots
```

`failed` у службы бывает в двух случаях: ошибка (нет сети, сервер
недоступен) или код 2, когда бэкап сделан, но снимок БД на сервере старше
`max_age_hours`. Второе значит, что на сервере не работает cron `backup_offsite`.

На сервере видно, когда агент последний раз забирал данные:
`instance/logs/backup_pull.log`.

## Восстановление

```bash
export RESTIC_REPOSITORY=/var/lib/coop-backup/restic RESTIC_PASSWORD_FILE=/etc/coop-backup/restic-password
sudo -E -u coop-backup restic snapshots
sudo -E restic restore latest --target /tmp/coop-restore   # или id снимка
```

В `/tmp/coop-restore` будет:

| Путь | Куда на новом сервере |
|---|---|
| `backup_staging/coop.db` | `instance/coop.db` |
| `backup_staging/env` | `.env` проекта |
| `uploads/`, `bank_certs/`, `bundles/` | `instance/` |
| `mail_backup/` | архив писем `.eml`, открывается любым почтовым клиентом; импорт в новый ящик — например `imapsync` или перетаскиванием в Thunderbird |

Новый сервер удобнее всего поднять ansible-плейбуком
(`deployment/ansible`): `restore_db_file` указывает на `coop.db`,
`restore_source_instance_dir` — на `/tmp/coop-restore`, а `SECRET_KEY` и
`BANK_API_ENCRYPTION_KEY` из `backup_staging/env` переносятся в `vars.yml`.

Тот же снимок есть во внешнем репозитории на Яндекс.Диске, см.
[docs/deployment.md](../docs/deployment.md), «Внешний бэкап».
