"""История баланса личного кабинета контрагента (counterparty_balance_snapshot)

Заполняется из журнала аудита (counterparty_api.balance_sync — запись
писалась при каждой смене баланса, сумма в конце текста) и текущего
external_balance, если в журнале его нет.

Revision ID: b6d2e8f4a1c9
Revises: a3e7c2f9b8d1
Create Date: 2026-11-27 12:00:00.000000

"""
import datetime as dt
import re
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6d2e8f4a1c9'
down_revision: Union[str, Sequence[str], None] = 'a3e7c2f9b8d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# audit.format_amount: "337,03 ₽", "-1234,50 ₽"
_AMOUNT_RE = re.compile(r"обновлён: (-?\d+,\d{2}) ₽\s*$")


def _as_datetime(value):
    # SQLite отдаёт DateTime сырой строкой
    return dt.datetime.fromisoformat(value) if isinstance(value, str) else value


def upgrade() -> None:
    op.create_table(
        'counterparty_balance_snapshot',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('counterparty_id', sa.Integer(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.ForeignKeyConstraint(['counterparty_id'], ['counterparty.id'],
                                name=op.f('fk_counterparty_balance_snapshot_counterparty_id_counterparty'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_counterparty_balance_snapshot')),
    )
    with op.batch_alter_table('counterparty_balance_snapshot', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_counterparty_balance_snapshot_counterparty_id'), ['counterparty_id'], unique=False)

    conn = op.get_bind()
    existing = {row[0] for row in conn.execute(sa.text("SELECT id FROM counterparty"))}
    rows = []
    last = {}  # последняя сумма по контрагенту — подряд идущие повторы не переносим
    for cp_id, created_at, summary in conn.execute(sa.text(
        "SELECT entity_id, created_at, summary FROM audit_log "
        "WHERE action = 'counterparty_api.balance_sync' AND entity_type = 'counterparty' "
        "ORDER BY created_at, id"
    )):
        m = _AMOUNT_RE.search(summary or "")
        if cp_id not in existing or not m:
            continue
        amount = Decimal(m.group(1).replace(",", "."))
        if last.get(cp_id) != amount:
            rows.append({"counterparty_id": cp_id, "recorded_at": _as_datetime(created_at), "amount": amount})
            last[cp_id] = amount
    for cp_id, amount, updated_at in conn.execute(sa.text(
        "SELECT id, external_balance, external_balance_updated_at FROM counterparty "
        "WHERE external_balance IS NOT NULL AND external_balance_updated_at IS NOT NULL"
    )):
        if last.get(cp_id) != Decimal(str(amount)):
            rows.append({"counterparty_id": cp_id, "recorded_at": _as_datetime(updated_at), "amount": Decimal(str(amount))})
    if rows:
        table = sa.table('counterparty_balance_snapshot',
                         sa.column('counterparty_id', sa.Integer), sa.column('recorded_at', sa.DateTime),
                         sa.column('amount', sa.Numeric(14, 2)))
        op.bulk_insert(table, rows)


def downgrade() -> None:
    with op.batch_alter_table('counterparty_balance_snapshot', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_counterparty_balance_snapshot_counterparty_id'))
    op.drop_table('counterparty_balance_snapshot')
