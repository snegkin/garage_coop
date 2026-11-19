"""
Самовыдвижение в выборные органы кооператива: правление, ревизионная
комиссия, председатель (ElectionCandidacy). Выдвигается сам член из
личного кабинета (cabinet.candidacy); список кандидатов виден всем
вошедшим на странице «Правление» и подсвечивается в списке людей при
внесении состава созыва/комиссии.

Право выдвигаться — собственник хотя бы одного гаража на территории
кооператива (за территорией — не члены, отдельный договор), не в архиве,
членство не прекращено. Проверяется и при подаче, и при показе списка.
"""
import datetime as dt

from sqlalchemy.orm import joinedload

from . import database
from .models import ElectionCandidacy, ElectionPosition, Garage, GarageOwnership, Person

# Порядок показа — от высшей должности.
POSITIONS = (ElectionPosition.CHAIRMAN, ElectionPosition.BOARD, ElectionPosition.REVISION_COMMISSION)

POSITION_LABELS = {
    ElectionPosition.CHAIRMAN: "Председатель кооператива",
    ElectionPosition.BOARD: "Член правления",
    ElectionPosition.REVISION_COMMISSION: "Член ревизионной комиссии",
}

# Для журнала: «выдвинул свою кандидатуру <куда>».
POSITION_AUDIT = {
    ElectionPosition.CHAIRMAN: "в председатели кооператива",
    ElectionPosition.BOARD: "в правление",
    ElectionPosition.REVISION_COMMISSION: "в ревизионную комиссию",
}


# Член ревизионной комиссии не может входить в правление (и быть
# председателем) — комиссия проверяет правление; поэтому и выдвигаться
# одновременно туда и туда нельзя. В уставе (2013 г.) ревизионная комиссия
# не упоминается вовсе — это общая практика (ср. ст. 120 ЖК для ЖСК), не
# норма устава.
BOARD_POSITIONS = frozenset({ElectionPosition.BOARD, ElectionPosition.CHAIRMAN})


def conflicting_positions(position: ElectionPosition) -> frozenset[ElectionPosition]:
    if position == ElectionPosition.REVISION_COMMISSION:
        return BOARD_POSITIONS
    return frozenset({ElectionPosition.REVISION_COMMISSION})


def eligible_person_ids() -> set[int]:
    today = dt.date.today()
    rows = (
        database.db_session.query(Person.id)
        .join(GarageOwnership, GarageOwnership.person_id == Person.id)
        .join(Garage, Garage.id == GarageOwnership.garage_id)
        .filter(Garage.outside_territory.is_(False), Person.is_archived.is_(False))
        .filter((Person.membership_end_date.is_(None)) | (Person.membership_end_date > today))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def is_eligible(person: Person | None) -> bool:
    return person is not None and person.id in eligible_person_ids()


def candidacies_by_position() -> dict[ElectionPosition, list[ElectionCandidacy]]:
    """Действующие заявки (только тех, кто сейчас вправе избираться), по должностям."""
    eligible = eligible_person_ids()
    result = {p: [] for p in POSITIONS}
    rows = (
        database.db_session.query(ElectionCandidacy)
        .options(joinedload(ElectionCandidacy.person))
        .order_by(ElectionCandidacy.created_at)
        .all()
    )
    for c in rows:
        if c.person_id in eligible:
            result[c.position].append(c)
    return result


def candidate_person_ids(*positions: ElectionPosition) -> set[int]:
    by_position = candidacies_by_position()
    return {c.person_id for p in positions for c in by_position[p]}
