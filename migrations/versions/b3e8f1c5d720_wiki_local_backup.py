"""wiki: local backup on the chairman's computer

Страница вики «Резервные копии на компьютере председателя» — памятка по
агенту agents/backup/ и командам restic. Только для правления (is_internal),
раздел «Сайт».

Разметка — под app/news_format.py: таблицы и ```-блоки (расширения
tables/fenced_code), абзацы одной строкой (nl2br превращает перенос строки
в <br>).

Revision ID: b3e8f1c5d720
Revises: a6d2f8c4b931
Create Date: 2026-11-17 23:50:00.000000

"""
import datetime as dt
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e8f1c5d720'
down_revision: Union[str, Sequence[str], None] = 'a6d2f8c4b931'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


WIKI_SLUG = "rezervnye-kopii-na-kompyutere-predsedatelya"
WIKI_TITLE = "Резервные копии на компьютере председателя"
PARENT_SLUG = "sait"
WIKI_BODY = r'''### Зачем эта копия

Данные сайта хранятся в трёх независимых местах:

| Копия | Где | От чего защищает |
|---|---|---|
| снимки БД | сам сервер, `instance/backups/` | порча базы, случайное удаление |
| внешняя (restic) | Яндекс.Диск | смерть сервера или хостера |
| **локальная (restic)** | **компьютер председателя** | смерть хостера и облака, взлом сервера |

Раз в сутки компьютер председателя сам забирает с сервера свежие данные (служба `coop-backup`, таймер `coop-backup.timer`) и кладёт их снимком в локальный restic-репозиторий. У сервера к этой копии доступа нет, поэтому при взломе сервера стереть её оттуда нельзя.

В каждом снимке лежат база данных, файл окружения `.env` (ключи `SECRET_KEY` и `BANK_API_ENCRYPTION_KEY`, без них сохранённые в базе пароли и токены не расшифровать), `uploads/`, `bank_certs/`, `bundles/` и архив почты правления `mail_backup/`.

### Где что лежит

| Что | Путь |
|---|---|
| конфиг агента | `/etc/coop-backup/coop_backup.ini` |
| пароль репозитория | `/etc/coop-backup/restic-password` |
| ssh-ключ агента | `/etc/coop-backup/id_ed25519` |
| restic-репозиторий (история) | `/var/lib/coop-backup/restic` |
| зеркало последнего состояния сервера | `/var/lib/coop-backup/mirror` |
| итог последнего прохода | `/var/lib/coop-backup/status.json` |
| программа агента | `/opt/coop-backup/coop_backup_agent.py` |

> **Пароль репозитория** должен храниться не только на этом компьютере, но и в менеджере паролей или на бумаге в сейфе. Если диск умрёт, копию репозитория без пароля не открыть.

Сколько снимков хранится (задаётся в конфиге, `keep_*`): 14 ежедневных, 8 еженедельных, 12 ежемесячных, 5 ежегодных. Лишние агент удаляет сам после каждого прохода (`forget --prune`).

### Подготовка: короткая команда для restic

Репозиторий принадлежит системному пользователю `coop-backup`. Все команды restic запускайте **от его имени**: если запустить `forget`, `prune` или `check` от root, в репозитории появятся файлы root, и агент потом не сможет их прочитать. Чтобы не набирать длинную строку, в начале сеанса задайте псевдоним:

```bash
alias rs='sudo -u coop-backup env RESTIC_REPOSITORY=/var/lib/coop-backup/restic RESTIC_PASSWORD_FILE=/etc/coop-backup/restic-password restic'
```

Дальше везде `rs` означает restic с этим репозиторием. Чтобы псевдоним был всегда, добавьте эту строку в `~/.bashrc`.

### Проверить, что бэкап работает

```bash
systemctl list-timers coop-backup.timer     # когда был последний запуск и когда следующий
systemctl status coop-backup                # итог последнего запуска
journalctl -u coop-backup -n 50             # журнал последнего прохода
sudo -u coop-backup python3 /opt/coop-backup/coop_backup_agent.py status
sudo systemctl start coop-backup            # снимок вне расписания (несколько минут)
```

Если служба в состоянии `failed`, причины две:

- **ошибка**: нет сети, сервер недоступен, кончилось место; текст ошибки виден в `journalctl`;
- **код 2**: снимок сделан, но база на сервере старше 36 часов. Значит, на сервере не работает cron `backup_offsite`, и чинить надо там.

На сервере видно, когда агент последний раз забирал данные: `instance/logs/backup_pull.log`.

### Просмотр снимков

```bash
rs snapshots                        # список снимков: ID, дата, размер
rs snapshots --latest 5             # последние 5
rs ls latest                        # файлы последнего снимка
rs ls latest /uploads               # содержимое одного каталога
rs ls 1a2b3c4d /backup_staging      # то же для конкретного снимка (ID из списка)
rs find '*.pdf'                     # в каких снимках встречается файл
rs diff 1a2b3c4d 5e6f7a8b           # что изменилось между двумя снимками
rs stats                            # размер данных в последнем состоянии
rs stats --mode raw-data            # сколько репозиторий занимает на диске
```

Пути внутри снимка начинаются с корня: `/backup_staging/coop.db` (база), `/backup_staging/env` (файл окружения), `/uploads/…`, `/bank_certs/…`, `/bundles/…`, `/mail_backup/…`.

Посмотреть снимки как обычные папки (`mount` только читает, поэтому его можно запускать от root):

```bash
sudo mkdir -p /mnt/restic
sudo env RESTIC_REPOSITORY=/var/lib/coop-backup/restic RESTIC_PASSWORD_FILE=/etc/coop-backup/restic-password restic mount /mnt/restic
# в другом терминале: sudo ls /mnt/restic/snapshots/   (папки по датам, latest — последний)
# закончить — Ctrl+C в первом терминале
```

### Управление историей

```bash
rs forget 1a2b3c4d --prune          # удалить один снимок и освободить место
rs forget --host coop-server --tag coop --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --keep-yearly 5 --dry-run
                                    # что удалила бы политика хранения (ничего не удаляет)
rs prune                            # убрать данные, на которые не ссылается ни один снимок
rs check                            # проверка структуры репозитория (быстро)
rs check --read-data-subset=10%     # плюс чтение 10 % данных (раз в месяц-два)
rs unlock                           # снять зависшую блокировку («repository is already locked»)
```

**Сохранить снимок навсегда**, например состояние перед годовым собранием или до крупного изменения на сайте. Агент чистит только снимки с меткой `coop`, поэтому снимок с другой меткой он не тронет:

```bash
rs tag --set archive 1a2b3c4d       # у снимка будет новый ID, посмотрите в rs snapshots
```

Вернуть снимок под общую политику: `rs tag --set coop <новый ID>`.

**Изменить, сколько хранить**: поменять `keep_daily`, `keep_weekly`, `keep_monthly`, `keep_yearly` в `/etc/coop-backup/coop_backup.ini` (`sudo nano /etc/coop-backup/coop_backup.ini`). Если агент ставился через ansible, править `local_backup_keep_*` в `vars.yml`, иначе плейбук вернёт старые значения. Новые значения применятся на следующем проходе.

**Место на диске**: `df -h /var/lib/coop-backup` и `rs stats --mode raw-data`. Снимки хранят только изменения, поэтому каждый новый обычно добавляет немного.

### Восстановление

**Один файл**, например базу на нужную дату, или один каталог:

```bash
rs snapshots                                                  # выбрать ID по дате
rs dump 1a2b3c4d /backup_staging/coop.db | sudo tee /tmp/coop.db > /dev/null
rs restore latest --target /tmp/coop-restore --include /uploads/news
```

**Всё целиком** (переезд на новый сервер):

```bash
rs restore latest --target /tmp/coop-restore      # или ID снимка вместо latest
sudo chown -R "$USER" /tmp/coop-restore           # чтобы файлы открывались без sudo
```

Что окажется в `/tmp/coop-restore` и куда это класть на новом сервере:

| В `/tmp/coop-restore` | На новом сервере |
|---|---|
| `backup_staging/coop.db` | `instance/coop.db` |
| `backup_staging/env` | `.env` проекта |
| `uploads/`, `bank_certs/`, `bundles/` | в `instance/` |
| `mail_backup/` | архив писем `.eml`: открывается любым почтовым клиентом, в новый ящик переносится через `imapsync` или перетаскиванием в Thunderbird |

Новый сервер проще всего поднять ansible-плейбуком (`deployment/ansible`): `restore_db_file` указывает на `coop.db`, `restore_source_instance_dir` на `/tmp/coop-restore`, а `SECRET_KEY` и `BANK_API_ENCRYPTION_KEY` из `backup_staging/env` переносятся в `vars.yml`.

> `/tmp` очищается при перезагрузке. Если восстановленное нужно сохранить, укажите в `--target` другой каталог.

### Если умер сам компьютер председателя

1. Сайт работает как прежде: копии на сервере и на Яндекс.Диске на месте.
2. Если репозиторий был на отдельном диске, подключите его к новому компьютеру и откройте тем же паролем: `sudo env RESTIC_REPOSITORY=/путь/к/restic RESTIC_PASSWORD_FILE=/путь/к/паролю restic snapshots`.
3. Агент на новом компьютере ставится по `agents/backup/README.md` или плейбуком (`local_backup_agent: true`, `deployment/setup.sh --tags local`). Новый ssh-ключ агента добавляется на сервер (`backup_agent_pubkeys` в `vars.yml`).

Подробно об установке: `agents/backup/README.md`. Про внешнюю копию: `docs/deployment.md`, раздел «Внешний бэкап».
'''


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(sa.text("SELECT id FROM wiki_page WHERE slug = :s"), {"s": WIKI_SLUG}).first()
    if exists is not None:
        return
    parent = conn.execute(sa.text("SELECT id FROM wiki_page WHERE slug = :s"), {"s": PARENT_SLUG}).first()
    conn.execute(
        sa.text("INSERT INTO wiki_page (title, slug, parent_id, body, is_internal, created_at) "
                "VALUES (:t, :s, :p, :b, 1, :at)"),
        {"t": WIKI_TITLE, "s": WIKI_SLUG, "p": parent[0] if parent else None, "b": WIKI_BODY,
         "at": dt.datetime.utcnow()},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM wiki_page WHERE slug = :s AND body = :b"), {"s": WIKI_SLUG, "b": WIKI_BODY})
