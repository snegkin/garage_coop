"""default notifications to email enabled

По просьбе пользователя — по умолчанию email-уведомления включены на все
события (см. app/models.py: User.notify_channel/notify_*, дефолты
изменены на EMAIL/True). Одних Python-дефолтов на mapped_column
недостаточно для уже существующих строк (они применяются только к новым
INSERT через ORM) — здесь SQL-бэкофилл существующих учётных записей плюс
server_default колонок на будущее, на случай прямой вставки в обход ORM.

Канал ставится в 'email' только тем, у чьего человека уже заполнен
email — иначе реальная отправка всё равно не пройдёт (notifications.
channel_is_ready), но выбранный в форме профиля канал выглядел бы
подтверждённым, хотя не подтверждён.

Revision ID: e379b7dc72bb
Revises: 602455be5ca3
Create Date: 2026-09-09 21:36:06.036702

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e379b7dc72bb'
down_revision: Union[str, Sequence[str], None] = '602455be5ca3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('notify_channel', server_default='EMAIL')
        batch_op.alter_column('notify_charge', server_default=sa.true())
        batch_op.alter_column('notify_payment', server_default=sa.true())
        batch_op.alter_column('notify_news', server_default=sa.true())
        batch_op.alter_column('notify_forum', server_default=sa.true())
        batch_op.alter_column('notify_board_chat', server_default=sa.true())

    op.execute("UPDATE user SET notify_charge = 1, notify_payment = 1, notify_news = 1, "
               "notify_forum = 1, notify_board_chat = 1")
    op.execute(
        "UPDATE user SET notify_channel = 'EMAIL' WHERE person_id IN ("
        "  SELECT id FROM person WHERE email IS NOT NULL AND TRIM(email) != ''"
        ")"
    )


def downgrade() -> None:
    """Downgrade schema.

    Лоссово для индивидуальных настроек, выставленных вручную ПОСЛЕ этой
    миграции (как и у любой data-миграции) — откатывает к состоянию "всё
    выключено", в котором были все учётные записи до неё.
    """
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('notify_channel', server_default=None)
        batch_op.alter_column('notify_charge', server_default=sa.false())
        batch_op.alter_column('notify_payment', server_default=sa.false())
        batch_op.alter_column('notify_news', server_default=sa.false())
        batch_op.alter_column('notify_forum', server_default=sa.false())
        batch_op.alter_column('notify_board_chat', server_default=sa.false())

    op.execute("UPDATE user SET notify_channel = NULL, notify_charge = 0, notify_payment = 0, "
               "notify_news = 0, notify_forum = 0, notify_board_chat = 0")
