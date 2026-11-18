"""
Запросы на изменение персональных данных из профиля (PersonDataRevision):
снимок карточки, применение, замена устаревших запросов и отмена одобрения.

Правила порядка:
- новый запрос человека заменяет его ещё не рассмотренные (SUPERSEDED) —
  форма профиля подставляет ожидающие данные, так что новый запрос уже
  включает всё из старого;
- одобрить запрос, после которого тот же человек отправил другой, нельзя —
  иначе, одобрив оба не по порядку, старые данные затрут новые;
- отменить можно только последнее одобрение по карточке: карточка
  возвращается к состоянию перед ним.
"""
import datetime as dt
import json

from . import database
from .models import Person, PersonDataRevision, PersonDataRevisionStatus, Phone

S = PersonDataRevisionStatus


def snapshot(person: Person) -> dict:
    """Поля, которые человек может менять из профиля, — в том же виде, что
    и fields_snapshot запроса."""
    return {
        "email": person.email,
        "telegram": person.telegram,
        "vk": person.vk,
        "max_messenger": person.max_messenger,
        "registration_address": person.registration_address,
        "residence_address": person.residence_address,
        "phones": sorted([p.number for p in person.phones]),
        "birth_date": person.birth_date.isoformat() if person.birth_date else None,
        "passport_series": person.passport_series,
        "passport_number": person.passport_number,
        "passport_issue_date": person.passport_issue_date.isoformat() if person.passport_issue_date else None,
        "membership_start_date": person.membership_start_date.isoformat() if person.membership_start_date else None,
    }


def _date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def apply_snapshot(person: Person, snap: dict) -> None:
    person.email = snap.get("email")
    person.telegram = snap.get("telegram")
    person.vk = snap.get("vk")
    person.max_messenger = snap.get("max_messenger")
    person.registration_address = snap.get("registration_address")
    person.residence_address = snap.get("residence_address")
    person.phones.clear()  # delete-orphan — строки удалятся при flush
    for number in snap.get("phones", []):
        person.phones.append(Phone(person_id=person.id, number=number))
    person.birth_date = _date(snap.get("birth_date"))
    person.passport_series = snap.get("passport_series")
    person.passport_number = snap.get("passport_number")
    person.passport_issue_date = _date(snap.get("passport_issue_date"))
    person.membership_start_date = _date(snap.get("membership_start_date"))


def _load(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def supersede_pending(person_id: int, older_than: dt.datetime | None = None) -> int:
    """Помечает ожидающие запросы человека заменёнными (все или только
    отправленные раньше older_than). Возвращает, сколько пометили."""
    query = database.db_session.query(PersonDataRevision).filter_by(person_id=person_id, status=S.PENDING)
    if older_than is not None:
        query = query.filter(PersonDataRevision.submitted_at < older_than)
    count = 0
    for revision in query:
        revision.status = S.SUPERSEDED
        revision.reviewed_at = dt.datetime.utcnow()
        count += 1
    return count


def newer_revision(revision: PersonDataRevision) -> PersonDataRevision | None:
    """Более поздний запрос того же человека, ожидающий или одобренный, —
    при нём этот запрос одобрять нельзя."""
    return (
        database.db_session.query(PersonDataRevision)
        .filter(
            PersonDataRevision.person_id == revision.person_id,
            PersonDataRevision.id != revision.id,
            PersonDataRevision.submitted_at > revision.submitted_at,
            PersonDataRevision.status.in_([S.PENDING, S.APPROVED]),
        )
        .order_by(PersonDataRevision.submitted_at.desc())
        .first()
    )


def approve(revision: PersonDataRevision, reviewer_id: int) -> None:
    person = revision.person
    snap = _load(revision.fields_snapshot)
    revision.applied_over_snapshot = json.dumps(snapshot(person), ensure_ascii=False)
    if snap is not None:
        apply_snapshot(person, snap)
    revision.status = S.APPROVED
    revision.reviewed_at = dt.datetime.utcnow()
    revision.reviewer_user_id = reviewer_id
    supersede_pending(person.id, older_than=revision.submitted_at)


def last_approved(person_id: int) -> PersonDataRevision | None:
    return (
        database.db_session.query(PersonDataRevision)
        .filter_by(person_id=person_id, status=S.APPROVED)
        .order_by(PersonDataRevision.reviewed_at.desc(), PersonDataRevision.id.desc())
        .first()
    )


def can_revert(revision: PersonDataRevision) -> bool:
    if revision.status != S.APPROVED:
        return False
    latest = last_approved(revision.person_id)
    return latest is not None and latest.id == revision.id


def _restore_target(revision: PersonDataRevision) -> dict | None:
    """Что вернуть в карточку. Снимок перед одобрением, если он есть; у
    одобренных раньше, чем он появился, — данные предыдущего одобренного
    запроса, иначе карточка на момент отправки этого запроса."""
    stored = _load(revision.applied_over_snapshot)
    if stored is not None:
        return stored
    previous = (
        database.db_session.query(PersonDataRevision)
        .filter(
            PersonDataRevision.person_id == revision.person_id,
            PersonDataRevision.id != revision.id,
            PersonDataRevision.status == S.APPROVED,
            PersonDataRevision.reviewed_at <= revision.reviewed_at,
        )
        .order_by(PersonDataRevision.reviewed_at.desc(), PersonDataRevision.id.desc())
        .first()
    )
    if previous is not None:
        return _load(previous.fields_snapshot)
    return _load(revision.previous_snapshot)


def revert(revision: PersonDataRevision, user_id: int) -> bool:
    """Отменяет последнее одобрение по карточке. False — отменить нечем
    (не последнее одобрение или не осталось данных, к которым вернуться)."""
    if not can_revert(revision):
        return False
    target = _restore_target(revision)
    if target is None:
        return False
    apply_snapshot(revision.person, target)
    revision.status = S.REVERTED
    revision.reverted_at = dt.datetime.utcnow()
    revision.reverted_by_user_id = user_id
    return True
