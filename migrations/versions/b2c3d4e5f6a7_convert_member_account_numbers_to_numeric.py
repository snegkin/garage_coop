"""convert member account numbers to numeric format

Old format: {type_code}{garage_number}{owner_index}
  - type_code: "1" (land_tax) or "2" (membership)
  - garage_number: text like "001" or "61а" (may contain letters)
  - owner_index: last owner_digits chars

New format: {type_code}{garage_id}{owner_index}
  - type_code: "1" or "2" (from fee_type.type_code)
  - garage_id: numeric ID from DB
  - owner_index: position of owner in ownerships ordered by GarageOwnership.id
  - penalty accounts get "П" prefix (from AccountNumberSettings.penalty_prefix)
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
    settings = conn.execute(sa.text(
        "SELECT garage_digits, owner_digits, penalty_prefix FROM account_number_settings LIMIT 1"
    )).fetchone()
    garage_digits = settings.garage_digits if settings else 3
    owner_digits = settings.owner_digits if settings else 1
    penalty_prefix = settings.penalty_prefix if settings else "П"

    # Build owner_index_by_garage_person from ownerships order
    garages = conn.execute(sa.text("SELECT id FROM garage")).fetchall()
    owner_index_map = {}
    for g in garages:
        ownerships = conn.execute(
            sa.text("SELECT person_id FROM garage_ownership WHERE garage_id = :gid ORDER BY id"),
            {"gid": g.id}
        ).fetchall()
        for idx, o in enumerate(ownerships):
            owner_index_map[(g.id, o.person_id)] = idx

    # Get all member accounts with fee_type info AND person_id
    accounts = conn.execute(sa.text("""
        SELECT ma.id, ma.account_number, ma.fee_type_id, ma.garage_id,
               ma.person_id, ft.type_code, ft.is_penalty
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

        type_code = str(acc.type_code) if acc.type_code else None
        if type_code is None:
            continue

        # Get owner_index from ownership order
        owner_idx = owner_index_map.get((acc.garage_id, acc.person_id), 0)

        # Build new number: {type_code}{garage_id.zfill}{owner_idx.zfill}
        garage_part = str(acc.garage_id).zfill(garage_digits)
        owner_part = str(owner_idx).zfill(owner_digits)
        base = f"{type_code}{garage_part}{owner_part}"
        new_number = f"{penalty_prefix}{base}" if acc.is_penalty else base

        if new_number != old_number:
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
    pass
