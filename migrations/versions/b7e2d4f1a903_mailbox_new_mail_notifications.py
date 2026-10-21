"""mailbox new mail notifications

Уведомления правлению о новых письмах в почте правления (см.
app/mailbox.py: refresh_unread_counts, app/notifications.py: группа
«mail»): MailboxSettings.known_message_ids — какие письма уже видели
на прошлом опросе. Членам правления, уже сохранившим свой выбор групп
уведомлений (User.notify_board_events не NULL), новая группа
добавляется включённой — иначе она досталась бы только тем, кто
настройки ещё не трогал.

Revision ID: b7e2d4f1a903
Revises: 276efa571cb9
Create Date: 2026-10-21 10:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2d4f1a903'
down_revision: Union[str, Sequence[str], None] = '276efa571cb9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _update_board_groups(change) -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, notify_board_events FROM \"user\" WHERE notify_board_events IS NOT NULL")).fetchall()
    for user_id, raw in rows:
        try:
            codes = json.loads(raw)
        except ValueError:
            continue
        new_codes = change(codes)
        if new_codes != codes:
            conn.execute(
                sa.text("UPDATE \"user\" SET notify_board_events = :value WHERE id = :id"),
                {"value": json.dumps(new_codes), "id": user_id},
            )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('known_message_ids', sa.Text(), nullable=True))
    _update_board_groups(lambda codes: codes if "mail" in codes else codes + ["mail"])


def downgrade() -> None:
    """Downgrade schema."""
    _update_board_groups(lambda codes: [code for code in codes if code != "mail"])
    with op.batch_alter_table('mailbox_settings', schema=None) as batch_op:
        batch_op.drop_column('known_message_ids')
