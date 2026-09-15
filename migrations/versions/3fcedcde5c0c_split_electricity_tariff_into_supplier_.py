"""split electricity tariff into supplier and member kinds

До этой миграции ElectricityTariff была одна история ставок,
использовавшаяся сразу для двух разных расчётов: сколько кооператив
должен поставщику (MasterMeterReading, показания общего счётчика) и
сколько платят члены кооператива за электричество в своих гаражах
(ElectricityReading/Charge). По прямой просьбе это разведено на два
независимых тарифа (см. ElectricityTariffKind в app/models.py) — у
поставщика и у кооператива теперь свои списки effective_date/rate,
можно менять независимо и указывать разные ставки (например, кооператив
берёт с членов чуть больше — на потери в сети, обслуживание и т.п.).

Бэкофилл: раньше единственная история тарифов ОДНОВРЕМЕННО была и
входящей (от поставщика), и исходящей (для членов) — весь прошлый расчёт
и по MasterMeterReading, и по ElectricityReading/Charge уже опирался на
одни и те же исторические ставки. Чтобы не потерять эту историю ни для
одной из сторон и не обнулить расчёт задним числом, каждая существующая
запись дублируется: исходная строка помечается kind='MEMBER' (тариф для
членов — на нём стоит server_default колонки), и для неё создаётся копия
с kind='SUPPLIER' (тариф поставщика) с теми же rate/effective_date/comment.
Дальше председатель заводит для каждого вида уже свою, независимую
историю на странице «Электроэнергия».

Revision ID: 3fcedcde5c0c
Revises: d1786b15be12
Create Date: 2026-09-14 22:02:31.778203

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3fcedcde5c0c'
down_revision: Union[str, Sequence[str], None] = 'd1786b15be12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    # server_default — только на время добавления колонки, чтобы заполнить
    # существующие строки (см. docstring выше); дальше, как и у остальных
    # Enum-колонок в проекте, значение всегда задаётся явно на уровне
    # приложения, держать server_default в схеме не нужно.
    with op.batch_alter_table('electricity_tariff', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'kind',
            sa.Enum('SUPPLIER', 'MEMBER', name='electricitytariffkind'),
            nullable=False, server_default='MEMBER',
        ))
        batch_op.create_index(batch_op.f('ix_electricity_tariff_kind'), ['kind'], unique=False)
    with op.batch_alter_table('electricity_tariff', schema=None) as batch_op:
        batch_op.alter_column('kind', server_default=None)

    duplicated = conn.execute(sa.text(
        "INSERT INTO electricity_tariff (kind, rate, effective_date, comment) "
        "SELECT 'SUPPLIER', rate, effective_date, comment FROM electricity_tariff WHERE kind = 'MEMBER'"
    )).rowcount
    print(f"Тариф поставщика: продублирована история из {duplicated} существующей записи(ей) прежнего единого тарифа.")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('electricity_tariff', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_electricity_tariff_kind'))
        batch_op.drop_column('kind')
    # Примечание: downgrade НЕ схлопывает обратно продублированные при
    # upgrade строки (записи с kind='SUPPLIER', созданные из истории
    # kind='MEMBER') — после удаления самой колонки kind они неотличимы от
    # исходных, а какая из пары "правильная" уже не определить однозначно.
    # Если нужно откатиться по-настоящему чисто — восстановите БД из бэкапа.
