# Агент локального бэкапа

Программа для компьютера председателя (Linux, systemd). Раз в сутки
забирает с сервера копии данных кооперативов и хранит их в локальном
restic-репозитории с историей версий.

Кооперативов может быть несколько: по секции `[coop:ИМЯ]` в конфиге
(через ansible — основной и все `coop_extra_instances`). Служба, таймер,
ключ ssh, пароль и репозиторий у всех одни. У каждого кооператива своё
зеркало `mirror/ИМЯ/` и свои снимки (`--host` в restic): у основного
`coop-server`, у остальных имя кооператива. Политика хранения действует
для каждого кооператива отдельно. Если один кооператив не забрался,
остальные всё равно сохраняются.

| Копия | Где | Что защищает |
|---|---|---|
| `instance/backups/` | сам сервер (`backup_db`) | от порчи БД, случайного удаления |
| внешний restic | Яндекс.Диск (`backup_offsite`) | от смерти сервера и хостера |
| **этот агент** | компьютер председателя | от смерти хостера и облака, от взлома сервера |

Что забирается: снимок БД, файл окружения (`.env` с `SECRET_KEY` и
`BANK_API_ENCRYPTION_KEY`, без них сохранённые в БД пароли и токены не
расшифровать), `uploads/`, `bank_certs/`, `bundles/` и архив почты
`mail_backup/`. Всё это на сервере готовят cron-задачи `backup_mailbox` и
`backup_offsite`, см. [docs/deployment.md](../../docs/deployment.md), «Внешний бэкап».

**Почему так:** агент сам ходит на сервер (компьютер за NAT), а у сервера
нет доступа к этой копии. Ключ агента на сервере ограничен
`scripts/backup_pull_gate.sh`: только rsync, только чтение, только `instance/`.

## Установка

**Через ansible** (если сервер разворачивается с этого же компьютера): в
`vars.yml` задать `local_backup_agent: true`, при отдельном диске — ещё
`local_backup_repository` и `local_backup_mount`. Затем выполнить
`deployment/setup.sh --tags local`. Шаги 1–6 ниже плейбук сделает
сам, включая первый проход. Руками останется сохранить пароль
репозитория: плейбук напомнит.

Вручную:

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
   У дополнительного кооператива строку добавить его пользователю, в
   гейт передать его `ENV_FILE`:
   ```
   command="ENV_FILE=/home/coop2/coop.env /home/coop/garage_coop/scripts/backup_pull_gate.sh",restrict ssh-ed25519 AAAA… coop-backup
   ```
   Через ansible: `backup_agent_pubkeys` в `vars.yml`, плейбук добавит строку сам.

   Ключ сервера в known_hosts агента:
   ```bash
   ssh-keyscan -p 22 coop.example.ru | sudo tee /etc/coop-backup/known_hosts
   ```
   Отпечаток сверить с `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` на сервере.
   Кириллический домен — в punycode, как в `host` конфига: ssh сравнивает
   имя буквально (`python3 -c "print('пример.рф'.encode('idna').decode())"`).
   Файл должен читаться пользователем `coop-backup`
   (`sudo chmod 0644 /etc/coop-backup/known_hosts`), иначе ssh молча его
   пропустит и ответит «Host key verification failed».

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
   Вписать `host` и по секции `[coop:ИМЯ]` на каждый кооператив. Репозиторий лучше держать на отдельном диске:
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
   Забирать агенту есть что только после первого запуска `backup_offsite`
   на сервере. Плейбук делает такой запуск сам, если задан
   `backup_agent_pubkeys`; без ansible запустите вручную:
   `scripts/backup_offsite.sh` под пользователем сайта.

## Где что лежит

| Что | Путь |
|---|---|
| конфиг агента | `/etc/coop-backup/coop_backup.ini` |
| пароль репозитория | `/etc/coop-backup/restic-password` |
| ssh-ключ агента | `/etc/coop-backup/id_ed25519` |
| restic-репозиторий (история) | `/var/lib/coop-backup/restic` (или `repository` из конфига) |
| зеркала последнего состояния сервера | `/var/lib/coop-backup/mirror/ИМЯ/` |
| итог последнего прохода | `/var/lib/coop-backup/status.json` |
| программа агента | `/opt/coop-backup/coop_backup_agent.py` |

Политика хранения — `keep_*` в конфиге (по умолчанию 14 ежедневных,
8 еженедельных, 12 ежемесячных, 5 ежегодных), для каждого кооператива
отдельно. Лишние снимки агент удаляет сам после каждого прохода (`forget`
по каждому кооперативу, затем один `prune`), но только с меткой `coop`.

## Повседневное

```bash
systemctl list-timers coop-backup.timer   # когда был последний запуск и когда следующий
systemctl status coop-backup              # итог последнего
journalctl -u coop-backup -n 50           # журнал последнего прохода
sudo -u coop-backup python3 /opt/coop-backup/coop_backup_agent.py status
sudo -u coop-backup python3 /opt/coop-backup/coop_backup_agent.py snapshots
sudo systemctl start coop-backup          # снимок вне расписания (несколько минут)
```

`failed` у службы бывает в двух случаях: ошибка хотя бы у одного
кооператива (нет сети, сервер недоступен, кончилось место; текст в
`journalctl`) или код 2, когда бэкап сделан, но снимок БД на сервере старше
`max_age_hours`. Второе значит, что на сервере не работает cron
`backup_offsite`, и чинить надо там. В `status.json` общий итог (`ok`,
`error`, `warning` в виде «имя: текст») и подробности по каждому
кооперативу в `coops`.

На сервере видно, когда агент последний раз забирал данные:
`instance/logs/backup_pull.log` каждого кооператива.

## Команды restic

Репозиторий принадлежит пользователю `coop-backup`. Команды restic,
которые пишут в репозиторий (`forget`, `prune`, `check`, `tag`, `unlock`),
запускать **от его имени**: от root в репозитории появятся файлы root, и
агент потом не сможет их прочитать. Удобно завести псевдоним (можно в
`~/.bashrc`):

```bash
alias rs='sudo -u coop-backup env RESTIC_REPOSITORY=/var/lib/coop-backup/restic RESTIC_PASSWORD_FILE=/etc/coop-backup/restic-password restic'
```

### Просмотр снимков

В репозитории снимки всех кооперативов. Кооператив выбирается через
`--host`: `coop-server` у основного, имя у остальных (`rs snapshots`
показывает снимки по группам). **`latest` без `--host` означает последний
снимок любого кооператива.**

```bash
rs snapshots                        # список снимков: ID, дата, размер, по кооперативам
rs snapshots --host coop-auto-moto-2   # только одного кооператива
rs snapshots --latest 5             # последние 5
rs ls latest --host coop-server     # файлы последнего снимка основного
rs ls latest --host coop-server /uploads   # содержимое одного каталога
rs ls 1a2b3c4d /backup_staging      # то же для конкретного снимка
rs find '*.pdf'                     # в каких снимках встречается файл
rs diff 1a2b3c4d 5e6f7a8b           # что изменилось между двумя снимками
rs stats                            # размер данных в последнем состоянии
rs stats --mode raw-data            # сколько репозиторий занимает на диске
```

Пути внутри снимка начинаются с корня: `/backup_staging/coop.db` (база),
`/backup_staging/env` (файл окружения), `/uploads/…`, `/bank_certs/…`,
`/bundles/…`, `/mail_backup/…`.

Снимки как обычные папки (`mount` только читает, поэтому можно от root):

```bash
sudo mkdir -p /mnt/restic
sudo env RESTIC_REPOSITORY=/var/lib/coop-backup/restic RESTIC_PASSWORD_FILE=/etc/coop-backup/restic-password restic mount /mnt/restic
# в другом терминале: sudo ls /mnt/restic/snapshots/   (папки по датам, latest — последний)
# закончить — Ctrl+C в первом терминале
```

### Управление историей

```bash
rs forget 1a2b3c4d --prune          # удалить один снимок и освободить место
rs forget --host coop-server --tag coop --group-by host,tags --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --keep-yearly 5 --dry-run
                                    # что удалила бы политика хранения (ничего не удаляет)
rs prune                            # убрать данные, на которые не ссылается ни один снимок
rs check                            # проверка структуры репозитория (быстро)
rs check --read-data-subset=10%     # плюс чтение 10 % данных (раз в месяц-два)
rs unlock                           # снять зависшую блокировку («repository is already locked»)
```

**Сохранить снимок навсегда** (перед годовым собранием, до крупного
изменения на сайте): агент чистит только снимки с меткой `coop`, поэтому
снимок с другой меткой он не тронет.

```bash
rs tag --set archive 1a2b3c4d       # у снимка будет новый ID, см. rs snapshots
rs tag --set coop <новый ID>        # вернуть под общую политику
```

**Изменить, сколько хранить:** `keep_*` в `/etc/coop-backup/coop_backup.ini`.
Если агент ставился через ansible — `local_backup_keep_*` в `vars.yml`,
иначе плейбук вернёт старые значения. Применится на следующем проходе.

**Место на диске:** `df -h /var/lib/coop-backup` и `rs stats --mode raw-data`.
Снимки хранят только изменения, поэтому каждый новый обычно добавляет немного.

## Восстановление

**Один файл или каталог**, например базу на нужную дату:

```bash
rs snapshots                                                  # выбрать ID по дате
rs dump 1a2b3c4d /backup_staging/coop.db | sudo tee /tmp/coop.db > /dev/null
rs restore latest --host coop-server --target /tmp/coop-restore --include /uploads/news
```

**Всё целиком** (переезд на новый сервер):

```bash
rs restore latest --host coop-server --target /tmp/coop-restore   # или ID снимка вместо latest
sudo chown -R "$USER" /tmp/coop-restore           # чтобы файлы открывались без sudo
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

`/tmp` очищается при перезагрузке: если восстановленное нужно сохранить,
укажите в `--target` другой каталог.

Тот же снимок есть во внешнем репозитории на Яндекс.Диске, см.
[docs/deployment.md](../../docs/deployment.md), «Внешний бэкап».

## Если умер сам компьютер председателя

1. Сайт работает как прежде: копии на сервере и на Яндекс.Диске на месте.
2. Если репозиторий был на отдельном диске, подключить его к новому
   компьютеру и открыть тем же паролем:
   `sudo env RESTIC_REPOSITORY=/путь/к/restic RESTIC_PASSWORD_FILE=/путь/к/паролю restic snapshots`.
3. Поставить агента заново (раздел «Установка» выше или плейбуком) и
   добавить новый ssh-ключ агента на сервер (`backup_agent_pubkeys` в
   `vars.yml`).

Та же памятка для правления — в вики, страница «Резервные копии на
компьютере председателя» (раздел «Сайт»).
