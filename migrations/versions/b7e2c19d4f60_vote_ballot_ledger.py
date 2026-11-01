"""vote ballot ledger

Неизменяемый журнал бюллетеней (hash-цепочка, см. app/vote_ledger.py):
таблица vote_ballot_event (только на добавление — UPDATE/DELETE запрещены
триггерами SQLite) и контрольные хеши у vote. Голосования, открытые в момент
миграции, получают genesis-хеш по текущей повестке/реестру и по событию
source='migrated' на каждый уже поданный бюллетень — дальше цепочка растёт
как обычно. Закрытые ранее голосования остаются без журнала.

Revision ID: b7e2c19d4f60
Revises: f2c7d4a9b318
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2c19d4f60'
down_revision: Union[str, Sequence[str], None] = 'f2c7d4a9b318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRIGGERS = (
    "CREATE TRIGGER trg_vote_ballot_event_no_update BEFORE UPDATE ON vote_ballot_event "
    "BEGIN SELECT RAISE(ABORT, 'vote_ballot_event is append-only'); END",
    "CREATE TRIGGER trg_vote_ballot_event_no_delete BEFORE DELETE ON vote_ballot_event "
    "BEGIN SELECT RAISE(ABORT, 'vote_ballot_event is append-only'); END",
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('vote', schema=None) as batch_op:
        batch_op.add_column(sa.Column('registry_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('genesis_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('final_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('final_hash_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('final_total_weight', sa.Numeric(precision=10, scale=5), nullable=True))

    op.create_table(
        'vote_ballot_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vote_id', sa.Integer(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('entered_by_user_id', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('weight', sa.Numeric(precision=8, scale=5), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('prev_hash', sa.String(length=64), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['vote_id'], ['vote.id'], name=op.f('fk_vote_ballot_event_vote_id_vote')),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_vote_ballot_event_person_id_person')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_vote_ballot_event')),
        sa.UniqueConstraint('vote_id', 'seq', name='uq_vote_ballot_event_vote_seq'),
    )
    with op.batch_alter_table('vote_ballot_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_vote_ballot_event_vote_id'), ['vote_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_vote_ballot_event_person_id'), ['person_id'], unique=False)

    _seal_open_votes()

    if op.get_bind().dialect.name == "sqlite":
        for ddl in TRIGGERS:
            op.execute(ddl)


def _enum_value(enum_cls, raw):
    """SQLAlchemy Enum хранит ИМЯ члена перечисления — приводим к .value."""
    try:
        return enum_cls[raw].value
    except KeyError:
        return enum_cls(raw).value


def _seal_open_votes() -> None:
    from decimal import Decimal
    from app import vote_ledger as L
    from app.models import VoteType, VoteChoice, VoteStatus

    bind = op.get_bind()
    vote_t = sa.table(
        'vote', sa.column('id', sa.Integer), sa.column('title', sa.String), sa.column('description', sa.Text),
        sa.column('voting_type', sa.String), sa.column('status', sa.String),
        sa.column('opens_at', sa.DateTime), sa.column('closes_at', sa.DateTime),
        sa.column('registry_hash', sa.String), sa.column('genesis_hash', sa.String),
    )
    question_t = sa.table(
        'vote_question', sa.column('id', sa.Integer), sa.column('vote_id', sa.Integer),
        sa.column('order', sa.Integer), sa.column('text', sa.Text), sa.column('majority_threshold', sa.Numeric(5, 4)),
    )
    ballot_t = sa.table(
        'vote_ballot', sa.column('question_id', sa.Integer), sa.column('person_id', sa.Integer),
        sa.column('choice', sa.String), sa.column('weight', sa.Numeric(8, 5)), sa.column('comment', sa.Text),
        sa.column('cast_at', sa.DateTime),
    )
    ownership_t = sa.table('garage_ownership', sa.column('person_id', sa.Integer), sa.column('share', sa.Numeric(6, 5)))
    event_t = sa.table(
        'vote_ballot_event', sa.column('vote_id', sa.Integer), sa.column('seq', sa.Integer),
        sa.column('person_id', sa.Integer), sa.column('entered_by_user_id', sa.Integer),
        sa.column('source', sa.String), sa.column('payload', sa.Text), sa.column('weight', sa.Numeric(8, 5)),
        sa.column('created_at', sa.DateTime), sa.column('prev_hash', sa.String), sa.column('hash', sa.String),
    )

    weights: dict[int, Decimal] = {}
    for pid, share in bind.execute(sa.select(ownership_t.c.person_id, ownership_t.c.share)):
        if share and Decimal(str(share)) > 0:
            weights[pid] = weights.get(pid, Decimal("0")) + Decimal(str(share))
    registry_hash = L.registry_hash_of(weights)

    open_names = {VoteStatus.OPEN.name, VoteStatus.OPEN.value}
    for v in bind.execute(sa.select(vote_t)).mappings().all():
        if v['status'] not in open_names:
            continue
        questions = bind.execute(
            sa.select(question_t).where(question_t.c.vote_id == v['id'])
        ).mappings().all()
        q_fields = [
            {"id": q['id'], "order": q['order'], "text": q['text'],
             "majority_threshold": L.dec(q['majority_threshold'], L._Q4)}
            for q in sorted(questions, key=lambda q: (q['order'], q['id']))
        ]
        v_fields = {
            "id": v['id'], "title": v['title'], "description": v['description'] or None,
            "voting_type": _enum_value(VoteType, v['voting_type']),
            "opens_at": L.ts(v['opens_at']), "closes_at": L.ts(v['closes_at']),
        }
        genesis = L.genesis_hash_of(v_fields, q_fields, registry_hash)
        bind.execute(
            vote_t.update().where(vote_t.c.id == v['id']).values(registry_hash=registry_hash, genesis_hash=genesis)
        )

        # Уже поданные бюллетени: одно событие на (человек, вес) — частичные
        # ручные подачи разных вопросов могли пройти с разным весом.
        q_ids = [q['id'] for q in questions]
        if not q_ids:
            continue
        groups: dict[tuple[int, str], list] = {}
        for b in bind.execute(sa.select(ballot_t).where(ballot_t.c.question_id.in_(q_ids))).mappings().all():
            groups.setdefault((b['person_id'], L.dec(b['weight'])), []).append(b)
        ordered = sorted(groups.items(), key=lambda kv: (max(b['cast_at'] for b in kv[1]), kv[0][0], kv[0][1]))
        prev, seq = genesis, 0
        for (pid, weight), ballots in ordered:
            seq += 1
            payload = L.payload_of(
                {b['question_id']: _enum_value(VoteChoice, b['choice']) for b in ballots},
                {b['question_id']: b['comment'] for b in ballots},
            )
            created_at = max(b['cast_at'] for b in ballots)
            h = L.event_hash_of(
                prev, vote_id=v['id'], seq=seq, person_id=pid, entered_by_user_id=None,
                source=L.SOURCE_MIGRATED, payload=payload, weight=weight, created_at=created_at,
            )
            bind.execute(event_t.insert().values(
                vote_id=v['id'], seq=seq, person_id=pid, entered_by_user_id=None, source=L.SOURCE_MIGRATED,
                payload=payload, weight=Decimal(weight), created_at=created_at, prev_hash=prev, hash=h,
            ))
            prev = h


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_vote_ballot_event_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_vote_ballot_event_no_delete")
    with op.batch_alter_table('vote_ballot_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_vote_ballot_event_person_id'))
        batch_op.drop_index(batch_op.f('ix_vote_ballot_event_vote_id'))
    op.drop_table('vote_ballot_event')
    with op.batch_alter_table('vote', schema=None) as batch_op:
        batch_op.drop_column('final_total_weight')
        batch_op.drop_column('final_hash_at')
        batch_op.drop_column('final_hash')
        batch_op.drop_column('genesis_hash')
        batch_op.drop_column('registry_hash')
