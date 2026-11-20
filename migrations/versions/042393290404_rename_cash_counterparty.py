"""Контрагент для наличных — без «за свой счёт»: «Оплачено наличными / не через банк»

Траты наличными часто не за свой счёт, а из кассы/собранных наличными
взносов; «за свой счёт» теперь отдельный способ — долг кооператива
человеку (к возмещению), см. app/year_report.py: add_expense.

Revision ID: 042393290404
Revises: 8ec5560bcdf0
Create Date: 2026-11-20 19:52:37.017187

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '042393290404'
down_revision: Union[str, Sequence[str], None] = '8ec5560bcdf0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_NAME = "Оплачено не через банк (наличные, за свой счёт)"
NEW_NAME = "Оплачено наличными / не через банк"
NEW_COMMENT = "Траты, оплаченные не со счёта кооператива: наличными, а иногда и за свой счёт."


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(sa.text("UPDATE counterparty SET name = :new, comment = :comment WHERE name = :old")
               .bindparams(new=NEW_NAME, old=OLD_NAME, comment=NEW_COMMENT))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("UPDATE counterparty SET name = :old WHERE name = :new").bindparams(new=NEW_NAME, old=OLD_NAME))
