"""wiki: local backup — all cooperatives of the server

Агент agents/backup/ теперь забирает все кооперативы сервера в один
restic-репозиторий, у каждого свой --host. Страница «Резервные копии на
компьютере председателя» (b3e8f1c5d720): у всех команд с latest — --host,
иначе latest значит последний снимок любого кооператива.

Правка по фрагментам: если правление уже поменяло абзац, он остаётся как
есть.

Revision ID: c7f2a9d4e1b6
Revises: b3e8f1a6c2d7
Create Date: 2026-11-27 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7f2a9d4e1b6'
down_revision: Union[str, Sequence[str], None] = 'b3e8f1a6c2d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


WIKI_SLUG = "rezervnye-kopii-na-kompyutere-predsedatelya"

REPLACEMENTS = [
    (
        "Раз в сутки компьютер председателя сам забирает с сервера свежие данные (служба `coop-backup`, таймер `coop-backup.timer`) и кладёт их снимком в локальный restic-репозиторий. У сервера к этой копии доступа нет, поэтому при взломе сервера стереть её оттуда нельзя.",
        "Раз в сутки компьютер председателя сам забирает с сервера свежие данные (служба `coop-backup`, таймер `coop-backup.timer`) и кладёт их снимком в локальный restic-репозиторий. У сервера к этой копии доступа нет, поэтому при взломе сервера стереть её оттуда нельзя.\n\n"
        "Если на сервере работают несколько кооперативов, агент за один проход забирает данные всех. Репозиторий у них общий, а снимки у каждого кооператива свои: их различает имя хоста (`--host`). У основного кооператива это `coop-server`, у остальных имя кооператива, например `coop-auto-moto-2`. Если не удалось забрать один кооператив, остальные всё равно сохраняются.",
    ),
    (
        "| зеркало последнего состояния сервера | `/var/lib/coop-backup/mirror` |",
        "| зеркала последнего состояния сервера | `/var/lib/coop-backup/mirror/ИМЯ/`, у каждого кооператива своё |",
    ),
    (
        "Лишние агент удаляет сам после каждого прохода (`forget --prune`).",
        "Считается для каждого кооператива отдельно. Лишние агент удаляет сам после каждого прохода.",
    ),
    (
        "- **ошибка**: нет сети, сервер недоступен, кончилось место; текст ошибки виден в `journalctl`;",
        "- **ошибка** хотя бы у одного кооператива: нет сети, сервер недоступен, кончилось место; текст ошибки виден в `journalctl`, в `status` видно, у какого кооператива;",
    ),
    (
        "На сервере видно, когда агент последний раз забирал данные: `instance/logs/backup_pull.log`.",
        "На сервере видно, когда агент последний раз забирал данные: `instance/logs/backup_pull.log` каждого кооператива.",
    ),
    (
        "### Просмотр снимков\n\n```bash\nrs snapshots                        # список снимков: ID, дата, размер\n",
        "### Просмотр снимков\n\n"
        "> **`latest` без `--host` означает последний снимок любого кооператива.** Поэтому в командах с `latest` всегда указывайте кооператив: `--host coop-server` для основного или имя кооператива для остальных. Список имён показывает `rs snapshots`.\n\n"
        "```bash\nrs snapshots                        # список снимков: ID, дата, размер, по кооперативам\n"
        "rs snapshots --host coop-auto-moto-2   # снимки одного кооператива\n",
    ),
    (
        "rs ls latest                        # файлы последнего снимка\nrs ls latest /uploads               # содержимое одного каталога\n",
        "rs ls latest --host coop-server     # файлы последнего снимка основного кооператива\nrs ls latest --host coop-server /uploads   # содержимое одного каталога\n",
    ),
    (
        "rs forget --host coop-server --tag coop --keep-daily 14",
        "rs forget --host coop-server --tag coop --group-by host,tags --keep-daily 14",
    ),
    (
        "rs restore latest --target /tmp/coop-restore --include /uploads/news",
        "rs restore latest --host coop-server --target /tmp/coop-restore --include /uploads/news",
    ),
    (
        "rs restore latest --target /tmp/coop-restore      # или ID снимка вместо latest",
        "rs restore latest --host coop-server --target /tmp/coop-restore   # или ID снимка вместо latest",
    ),
    (
        "Новый ssh-ключ агента добавляется на сервер (`backup_agent_pubkeys` в `vars.yml`).",
        "Плейбук сам добавит новый ssh-ключ агента на сервер всем кооперативам; при установке без плейбука ключ добавляется через `backup_agent_pubkeys` в `vars.yml`.",
    ),
]


def _apply(pairs) -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT id, body FROM wiki_page WHERE slug = :s"), {"s": WIKI_SLUG}).first()
    if row is None or not row[1]:
        return
    body = row[1]
    for old, new in pairs:
        if old in body and new not in body:
            body = body.replace(old, new, 1)
    if body != row[1]:
        conn.execute(sa.text("UPDATE wiki_page SET body = :b WHERE id = :i"), {"b": body, "i": row[0]})


def upgrade() -> None:
    _apply(REPLACEMENTS)


def downgrade() -> None:
    _apply([(new, old) for old, new in reversed(REPLACEMENTS)])
