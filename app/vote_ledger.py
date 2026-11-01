"""
Неизменяемый журнал бюллетеней голосования (hash-цепочка) — защита от
подделки голосов задним числом без публикации персональных данных.

Идея: наружу (членам кооператива — в уведомлениях, в протокол) уходят только
хеши, по которым ничего нельзя узнать о том, кто как голосовал, но которые
однозначно фиксируют состояние:

- registry_hash — снимок реестра голосующих (person_id → вес) на момент
  открытия голосования;
- genesis_hash — повестка (вопросы, пороги), параметры голосования и
  registry_hash; считается при открытии и рассылается вместе с
  уведомлением об открытии;
- каждая подача бюллетеня — строка VoteBallotEvent с hash =
  sha256(prev_hash + канонический JSON полей), prev_hash первой = genesis;
- final_hash — голова цепочки + итоги (суммы по вариантам, явка, кворум,
  общий вес на момент закрытия); считается при закрытии и рассылается всем
  членам с правом голоса, председатель вписывает его в протокол.

verify() пересчитывает всё заново: повестку против genesis_hash, цепочку
событий, совпадение текущих VoteBallot с «проигранными» событиями и
final_hash. Правка vote_ballot/vote_question/vote в БД, удаление или вставка
события — всё это всплывает расхождением. Подменить цепочку ЦЕЛИКОМ с
пересчётом хешей можно, но тогда хеш не совпадёт с тем, что лежит у членов в
Telegram/почте.

Что журнал НЕ закрывает: тот, у кого есть доступ к БД, пока голосование
ОТКРЫТО, может переписать цепочку до рассылки final_hash. Против этого
нужны квитанции голосующим (отложено).

ФОРМАТ ХЕШИРОВАНИЯ ЗАМОРОЖЕН: любое изменение canonical()/полей хеша
сделает все существующие журналы «битыми». Если понадобится менять —
вводить новую версию ("v") и считать старые по старым правилам.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from . import database
from .models import Vote, VoteBallotEvent, VoteChoice, VoteStatus, GarageOwnership

LEDGER_VERSION = 1

SOURCE_SELF = "self"        # член кооператива сам, через сайт
SOURCE_MANUAL = "manual"    # председатель за члена (бумажный бюллетень очно-заочного)
SOURCE_MIGRATED = "migrated"  # перенос уже поданных голосов при внедрении журнала

_Q5 = Decimal("0.00001")
_Q4 = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Чистые функции хеширования — без БД, используются и миграцией
# ---------------------------------------------------------------------------

def dec(value, quant: Decimal = _Q5) -> str:
    """Decimal → строка с фиксированным числом знаков — чтобы 1, 1.0 и
    1.00000 (как их вернёт SQLite через Numeric) хешировались одинаково."""
    return str(Decimal(str(value)).quantize(quant))


def ts(value: dt.datetime | None) -> str | None:
    return value.isoformat(sep=" ") if value is not None else None


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.hexdigest()


def registry_hash_of(weights: dict[int, Decimal]) -> str:
    items = sorted((int(pid), dec(w)) for pid, w in weights.items() if w and Decimal(str(w)) > 0)
    return _sha(canonical({"v": LEDGER_VERSION, "registry": items}))


def genesis_hash_of(vote_fields: dict, questions: list[dict], registry_hash: str) -> str:
    return _sha(canonical({
        "v": LEDGER_VERSION, "vote": vote_fields, "questions": questions, "registry_hash": registry_hash,
    }))


def payload_of(choices: dict[int, VoteChoice | str], comments: dict[int, str | None]) -> str:
    """{question_id: [choice, comment|null]} канонической JSON-строкой — в
    таком виде хранится в VoteBallotEvent.payload и входит в хеш."""
    data = {}
    for qid, choice in choices.items():
        value = choice.value if isinstance(choice, VoteChoice) else str(choice)
        data[str(int(qid))] = [value, comments.get(qid) or None]
    return canonical(data).decode("utf-8")


def event_hash_of(prev_hash: str, *, vote_id: int, seq: int, person_id: int, entered_by_user_id: int | None,
                  source: str, payload: str, weight, created_at: dt.datetime) -> str:
    fields = {
        "v": LEDGER_VERSION, "vote_id": vote_id, "seq": seq, "person_id": person_id,
        "entered_by_user_id": entered_by_user_id, "source": source, "payload": payload,
        "weight": dec(weight), "created_at": ts(created_at),
    }
    return _sha(prev_hash.encode("ascii"), b"\n", canonical(fields))


def final_hash_of(head_hash: str, results: dict) -> str:
    return _sha(head_hash.encode("ascii"), b"\n", canonical({"v": LEDGER_VERSION, "results": results}))


# ---------------------------------------------------------------------------
# Работа с моделями
# ---------------------------------------------------------------------------

def registry_snapshot() -> dict[int, Decimal]:
    """Текущий реестр голосующих: person_id → сумма долей (как person_voting_weight)."""
    weights: dict[int, Decimal] = {}
    for o in database.db_session.query(GarageOwnership).all():
        if o.share and o.share > 0:
            weights[o.person_id] = weights.get(o.person_id, Decimal("0")) + o.share
    return weights


def _vote_fields(vote: Vote) -> dict:
    return {
        "id": vote.id, "title": vote.title, "description": vote.description or None,
        "voting_type": vote.voting_type.value, "opens_at": ts(vote.opens_at), "closes_at": ts(vote.closes_at),
    }


def _question_fields(vote: Vote) -> list[dict]:
    return [
        {"id": q.id, "order": q.order, "text": q.text, "majority_threshold": dec(q.majority_threshold, _Q4)}
        for q in sorted(vote.questions, key=lambda q: (q.order, q.id))
    ]


def compute_genesis(vote: Vote) -> str:
    return genesis_hash_of(_vote_fields(vote), _question_fields(vote), vote.registry_hash or "")


def seal_on_open(vote: Vote) -> None:
    """Фиксирует реестр голосующих и повестку — вызывать при draft → open, до commit."""
    database.db_session.flush()  # id вопросов нужны в хеше
    vote.registry_hash = registry_hash_of(registry_snapshot())
    vote.genesis_hash = compute_genesis(vote)


def _events(vote: Vote) -> list[VoteBallotEvent]:
    return (
        database.db_session.query(VoteBallotEvent)
        .filter_by(vote_id=vote.id)
        .order_by(VoteBallotEvent.seq)
        .all()
    )


def _head(vote: Vote) -> tuple[int, str]:
    last = (
        database.db_session.query(VoteBallotEvent)
        .filter_by(vote_id=vote.id)
        .order_by(VoteBallotEvent.seq.desc())
        .first()
    )
    if last is None:
        return 0, vote.genesis_hash
    return last.seq, last.hash


def append_event(vote: Vote, person_id: int, choices: dict[int, VoteChoice], comments: dict[int, str | None],
                 weight: Decimal, source: str, entered_by_user_id: int | None, created_at: dt.datetime) -> None:
    """Дописывает подачу бюллетеня в цепочку. Ничего не делает для
    голосований без журнала (genesis_hash не задан — открыто до внедрения).
    Гонку двух одновременных подач закрывает UniqueConstraint(vote_id, seq):
    вторая упадёт IntegrityError, а не создаст развилку цепочки."""
    if not vote.genesis_hash or not choices:
        return
    database.db_session.flush()  # autoflush выключен — иначе не видна ещё не записанная предыдущая подача
    seq, prev_hash = _head(vote)
    seq += 1
    payload = payload_of(choices, comments)
    database.db_session.add(VoteBallotEvent(
        vote_id=vote.id, seq=seq, person_id=person_id, entered_by_user_id=entered_by_user_id,
        source=source, payload=payload, weight=weight, created_at=created_at, prev_hash=prev_hash,
        hash=event_hash_of(
            prev_hash, vote_id=vote.id, seq=seq, person_id=person_id, entered_by_user_id=entered_by_user_id,
            source=source, payload=payload, weight=weight, created_at=created_at,
        ),
    ))


def results_for_hash(vote: Vote) -> dict:
    """Итоги, входящие в final_hash: по вопросам суммы весов и решение, явка,
    кворум, общий вес. Считаются той же логикой, что показывается на сайте."""
    from .voting import question_results, vote_participation_weight, quorum_met, effective_total_weight
    questions = []
    for q in sorted(vote.questions, key=lambda q: (q.order, q.id)):
        r = question_results(q)
        questions.append({
            "id": q.id, "for": dec(r["for"]), "against": dec(r["against"]), "abstain": dec(r["abstain"]),
            "passed": bool(r["passed"]),
        })
    return {
        "questions": questions,
        "participation": dec(vote_participation_weight(vote)),
        "total_weight": dec(effective_total_weight(vote)),
        "quorum": bool(quorum_met(vote)),
    }


def compute_final(vote: Vote) -> str:
    _, head = _head(vote)
    return final_hash_of(head, results_for_hash(vote))


def seal_on_close(vote: Vote, total_weight: Decimal) -> None:
    """Фиксирует общий вес и итоговый хеш — вызывать при open → closed, до commit."""
    if not vote.genesis_hash:
        return
    database.db_session.flush()
    vote.final_total_weight = Decimal(dec(total_weight))
    vote.final_hash = compute_final(vote)
    vote.final_hash_at = dt.datetime.now()


# ---------------------------------------------------------------------------
# Проверка целостности
# ---------------------------------------------------------------------------

@dataclass
class Problem:
    text: str                      # без персональных данных — можно показывать всем
    person_id: int | None = None   # для правления — к кому относится расхождение


@dataclass
class VerifyResult:
    has_ledger: bool
    events: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.has_ledger and not self.problems


def verify(vote: Vote) -> VerifyResult:
    if not vote.genesis_hash:
        return VerifyResult(has_ledger=False)
    database.db_session.flush()
    result = VerifyResult(has_ledger=True)
    problems = result.problems
    qnum = {q.id: i for i, q in enumerate(sorted(vote.questions, key=lambda q: (q.order, q.id)), start=1)}

    if compute_genesis(vote) != vote.genesis_hash:
        problems.append(Problem("Повестка или параметры голосования не совпадают с зафиксированными при открытии (genesis-хеш)."))

    # Цепочка
    events = _events(vote)
    result.events = len(events)
    expected_prev = vote.genesis_hash
    state: dict[tuple[int, int], tuple[str, str | None, str]] = {}
    expected_seq = 1
    for e in events:
        if e.seq != expected_seq:
            if e.seq > expected_seq:
                span = f"№{expected_seq}" if e.seq - 1 == expected_seq else f"№{expected_seq}–{e.seq - 1}"
                problems.append(Problem(f"Отсутствуют записи журнала {span} — удалены после записи."))
            else:
                problems.append(Problem(f"Нарушен порядок записей журнала (№{e.seq})."))
        expected_seq = e.seq + 1
        if e.prev_hash != expected_prev:
            problems.append(Problem(f"Запись журнала №{e.seq} не ссылается на предыдущую — цепочка разорвана.", e.person_id))
        recomputed = event_hash_of(
            e.prev_hash, vote_id=e.vote_id, seq=e.seq, person_id=e.person_id,
            entered_by_user_id=e.entered_by_user_id, source=e.source, payload=e.payload,
            weight=e.weight, created_at=e.created_at,
        )
        if recomputed != e.hash:
            problems.append(Problem(f"Запись журнала №{e.seq} изменена после записи (хеш не совпадает).", e.person_id))
        expected_prev = e.hash
        try:
            data = json.loads(e.payload)
        except ValueError:
            problems.append(Problem(f"Запись журнала №{e.seq} повреждена.", e.person_id))
            continue
        for qid, (choice, comment) in data.items():
            state[(e.person_id, int(qid))] = (choice, comment or None, dec(e.weight))

    # Текущие бюллетени против проигранного журнала
    current: dict[tuple[int, int], tuple[str, str | None, str]] = {}
    for q in vote.questions:
        for b in q.ballots:
            current[(b.person_id, q.id)] = (b.choice.value, b.comment or None, dec(b.weight))
    for key in sorted(set(state) | set(current)):
        pid, qid = key
        label = f"вопрос {qnum.get(qid, qid)}"
        if key not in current:
            problems.append(Problem(f"Голос из журнала отсутствует в бюллетенях ({label}).", pid))
        elif key not in state:
            problems.append(Problem(f"В бюллетенях есть голос, которого нет в журнале ({label}).", pid))
        elif state[key] != current[key]:
            problems.append(Problem(f"Голос в бюллетенях не совпадает с журналом ({label}).", pid))

    if vote.status == VoteStatus.CLOSED:
        if not vote.final_hash:
            problems.append(Problem("Голосование закрыто, но итоговый хеш не зафиксирован."))
        elif compute_final(vote) != vote.final_hash:
            problems.append(Problem("Итоги или журнал не совпадают с итоговым хешем, зафиксированным при закрытии."))
    return result
