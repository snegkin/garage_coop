"""Договоры на электроэнергию: лицевой счёт в реквизитах стороны

Revision ID: e9c4f2b6d531
Revises: d7b3e1a5c429
Create Date: 2026-11-16 22:30:00

В уже добавленных стандартных договорах (по названию) в реквизиты
Пользователя / Члена кооператива добавляется «Лицевой счёт №
{{лицевой_счёт_гаража}}» — только если фрагмент стандартный.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9c4f2b6d531'
down_revision: Union[str, Sequence[str], None] = 'd7b3e1a5c429'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Название шаблона → [(было, стало), …].
REPLACEMENTS = {'Договор на электроэнергию (не член кооператива)': [('_________________}}</p></td></tr><tr><td', '_________________}}<br>Лицевой счёт № {{лицевой_счёт_гаража}}</p></td></tr><tr><td')], 'Договор на электроэнергию (член кооператива)': [('_________________}}</p></td></tr><tr><td', '_________________}}<br>Лицевой счёт № {{лицевой_счёт_гаража}}</p></td></tr><tr><td')]}


def _apply(reverse: bool) -> None:
    bind = op.get_bind()
    for name, pairs in REPLACEMENTS.items():
        row = bind.execute(sa.text("SELECT id, body_html FROM document_template WHERE name = :n"), {"n": name}).first()
        if row is None:
            continue
        body = row.body_html or ""
        for old, new in pairs:
            body = body.replace(new, old) if reverse else body.replace(old, new)
        if body != row.body_html:
            bind.execute(sa.text("UPDATE document_template SET body_html = :b WHERE id = :i"), {"b": body, "i": row.id})


def upgrade() -> None:
    _apply(reverse=False)


def downgrade() -> None:
    _apply(reverse=True)
