"""add slug to wiki page

Человекопонятный URL страницы вики (/wiki/<slug> вместо /wiki/<id>) — см.
app/models.py:WikiPage.slug, app/wiki.py:_slugify/_unique_slug. Старый
числовой /wiki/<id> продолжает работать (редирект на канонический адрес,
см. wiki.py:view_by_id) — уже разошедшиеся где-то ссылки не ломаются.

Бэкофилл данных: для каждой уже существующей страницы slug строится из её
title (транслитерация кириллицы в латиницу, см. app/translit.py —
таблица здесь продублирована самостоятельно, не импортируется из app/,
чтобы миграция не зависела от кода, который может поменяться позже) с
проверкой уникальности (коллизия -> числовой суффикс -2, -3...). Порядок
обхода — по id, тем же порядком, что страницы создавались — при коллизии
более старая страница получает "чистый" slug без суффикса.

Revision ID: 1d2c69d8ddb7
Revises: 19bf37b971fd
Create Date: 2026-09-06 12:16:38.249187

"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1d2c69d8ddb7'
down_revision: Union[str, Sequence[str], None] = '19bf37b971fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    translit = "".join(_TRANSLIT_MAP.get(ch, ch) for ch in (title or "").lower())
    slug = _SLUG_NON_ALNUM_RE.sub("-", translit).strip("-")
    return slug or "page"


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    # Шаг 1: колонка сначала nullable — заполним значения бэкофиллом,
    # прежде чем требовать NOT NULL + UNIQUE (иначе на непустой таблице
    # это упало бы сразу же).
    with op.batch_alter_table('wiki_page', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slug', sa.String(length=255), nullable=True))

    rows = conn.execute(sa.text("SELECT id, title FROM wiki_page ORDER BY id")).fetchall()
    used_slugs: set[str] = set()
    for row in rows:
        base_slug = _slugify(row.title)
        slug = base_slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        used_slugs.add(slug)
        conn.execute(sa.text("UPDATE wiki_page SET slug = :slug WHERE id = :id"), {"slug": slug, "id": row.id})

    # Шаг 2: теперь можно требовать NOT NULL + UNIQUE.
    with op.batch_alter_table('wiki_page', schema=None) as batch_op:
        batch_op.alter_column('slug', existing_type=sa.String(length=255), nullable=False)
        batch_op.create_index(batch_op.f('ix_wiki_page_slug'), ['slug'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('wiki_page', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_page_slug'))
        batch_op.drop_column('slug')
