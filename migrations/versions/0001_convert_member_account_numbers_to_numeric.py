"""convert member account numbers to numeric format

Old format: {type_code}{garage_number}{owner_index}
  - type_code: "1" (land_tax) or "2" (membership)
  - garage_number: text like "001" or "61а" (may contain letters)
  - owner_index: last digit

New format: {fee_type_id}{garage_id}{owner_index}
  - fee_type_id: 2, 3, 4, or 5
  - garage_id: numeric ID from DB
  - owner_index: same as before
  - penalty accounts get "П" prefix

Mapping:
  type_code=1, is_penalty=False  -> fee_type.id=2
  type_code=1, is_penalty=True   -> fee_type.id=3
  type_code=2, is_penalty=False  -> fee_type.id=4
  type_code=2, is_penalty=True   -> fee_type.id=5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = '2a97e92adbab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Get settings
    settings = conn.execute(sa.text("SELECT garage_digits, owner_digits FROM account_number_settings LIMIT 1")).fetchone()
    garage_digits = settings.garage_digits if settings else 3
    owner_digits = settings.owner_digits if settings else 1

    # Get fee_type mapping: (type_code, is_penalty) -> id
    rows = conn.execute(
        sa.text("SELECT id, type_code, is_penalty FROM fee_type WHERE type_code IS NOT NULL")
    ).fetchall()
    fee_type_map = {}
    for r in rows:
        if r.type_code is not None:
            fee_type_map[(str(r.type_code), bool(r.is_penalty))] = r.id

    # Get all member accounts with their fee_type info
    accounts = conn.execute(sa.text("""
        SELECT ma.id, ma.account_number, ma.fee_type_id, ma.garage_id,
               ft.type_code, ft.is_penalty
        FROM member_account ma
        JOIN fee_type ft ON ma.fee_type_id = ft.id
        WHERE ft.type_code IS NOT NULL
    """)).fetchall()

    changed = 0
    failed = 0

    for acc in accounts:
        old_number = acc.account_number
        if not old_number:
            continue

        type_code = str(acc.type_code)
        is_penalty = bool(acc.is_penalty)

        new_fee_type_id = fee_type_map.get((type_code, is_penalty))
        if new_fee_type_id is None:
            continue

        # Extract owner_index from last owner_digits chars
        if len(old_number) >= owner_digits:
            owner_part = old_number[-owner_digits:]
        else:
            owner_part = '0'
        try:
            owner_idx = int(owner_part)
        except ValueError:
            owner_idx = 0

        # Use garage_id (numeric) instead of garage_number (text with possible letters)
        garage_digits_str = str(acc.garage_id)
        base = f"{new_fee_type_id}{garage_digits_str.zfill(garage_digits)}{str(owner_idx).zfill(owner_digits)}"
        new_number = base if not is_penalty else f"П{base}"

        if new_number != old_number:
            # Check for conflicts
            conflict = conn.execute(
                sa.text("SELECT id FROM member_account WHERE account_number = :num AND id != :id"),
                {"num": new_number, "id": acc.id}
            ).fetchone()
            if conflict:
                failed += 1
                continue
            conn.execute(
                sa.text("UPDATE member_account SET account_number = :num WHERE id = :id"),
                {"num": new_number, "id": acc.id}
            )
            changed += 1

    print(f"Converted: {changed}, Conflicts: {failed}")


def downgrade() -> None:
    # Cannot reliably downgrade — old format is lost
    pass
