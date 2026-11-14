"""Шаблоны: заявления — «В правление …», «от» в родительном; уведомление — «кому» в дательном, обращение по имени-отчеству

Revision ID: e2c7a9f1b468
Revises: d4f9b2e7a315
Create Date: 2026-11-14 16:00:00

Уже добавленные стандартные шаблоны заявлений (app/documents.py:
_TO_BOARD/_FROM_PERSON): заменяется только точный стандартный текст шапки,
правленные вручную шапки не трогаются. Документы в журнале — как были.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2c7a9f1b468'
down_revision: Union[str, Sequence[str], None] = 'd4f9b2e7a315'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TO = "<p>Председателю правления<br>{{кооператив_кратко}}<br>{{председатель_кратко}}</p>"
NEW_TO = "<p>В правление {{кооператив_кратко}}</p>"
OLD_FROM = "<p>от {{ФИО}}</p>"
NEW_FROM = "<p>от {{ФИО_от_кого}}</p>"
# Уведомление о задолженности: шапка «кому» — в дательном, обращение —
# «Уважаемая Анна Ивановна!» вместо «Уважаемый(ая) Петрова Анна Ивановна!».
OLD_TO_PERSON = "<p>{{ФИО}}</p><p>{{адрес}}</p>"
NEW_TO_PERSON = "<p>{{ФИО_кому}}</p><p>{{адрес}}</p>"
OLD_GREETING = "Уважаемый(ая) {{ФИО}}!"
NEW_GREETING = "{{уважаемый}} {{имя_отчество}}!"


def _swap(column: str, pairs) -> None:
    for old, new in pairs:
        op.get_bind().execute(
            sa.text(f"UPDATE document_template SET {column} = replace({column}, :old, :new) "
                    f"WHERE {column} LIKE '%' || :old || '%'"),
            {"old": old, "new": new},
        )


def upgrade() -> None:
    _swap("addressee_html", [(OLD_TO, NEW_TO), (OLD_FROM, NEW_FROM), (OLD_TO_PERSON, NEW_TO_PERSON)])
    _swap("body_html", [(OLD_GREETING, NEW_GREETING)])


def downgrade() -> None:
    _swap("addressee_html", [(NEW_TO, OLD_TO), (NEW_FROM, OLD_FROM), (NEW_TO_PERSON, OLD_TO_PERSON)])
    _swap("body_html", [(NEW_GREETING, OLD_GREETING)])
