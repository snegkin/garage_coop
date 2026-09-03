"""
Проверки доступа к конкретным объектам (свой это гараж/счёт/карточка или
нет) — раньше были продублированы по отдельности в garages.py, persons.py,
finance.py и pd4.py (местами под разными именами: _is_board_role() /
_is_board() — одно и то же; _is_owner_or_board() была скопирована дважды
дословно; в pd4.py вдобавок висела неиспользуемая копия). Здесь — по одной
версии каждой проверки, импортируется во все модули маршрутов.

Ограничение доступа к роуту ЦЕЛИКОМ по минимальной роли (декоратор
@roles_required, ROLE_LEVEL) остаётся в auth.py — это про вход в сам роут;
здесь — про доступ к конкретному объекту уже внутри открытого роута.
"""
from flask import g

from .auth import ROLE_LEVEL
from .models import RoleEnum


def is_board() -> bool:
    """Правление, бухгалтер или председатель — роль не ниже BOARD (см. auth.ROLE_LEVEL)."""
    return ROLE_LEVEL[g.user.role] >= ROLE_LEVEL[RoleEnum.BOARD]


def is_chairman() -> bool:
    """Только председатель — самая узкая проверка, для действий вроде правки чужих
    ошибок (последнее показание счётчика) или назначения нового председателя."""
    return g.user.role == RoleEnum.CHAIRMAN


def is_privileged() -> bool:
    """
    Только председатель или бухгалтер. Рядовой член правления (BOARD) НЕ
    считается — в отличие от is_board(). Не выражается через ROLE_LEVEL
    (BOARD и ACCOUNTANT на одном уровне доступа) — отдельный, более узкий
    набор ролей, используется там, где обычному члену правления видеть
    что-то ещё не положено (например, строки «пеня» на некоторых экранах).
    """
    return g.user.role in (RoleEnum.CHAIRMAN, RoleEnum.ACCOUNTANT)


def is_owner_or_board(garage) -> bool:
    """Правление/бухгалтер/председатель — любой гараж; рядовой член — только свой (по владению)."""
    if is_board():
        return True
    if g.user.person_id is None:
        return False
    return g.user.person_id in {o.person_id for o in garage.ownerships}


def can_view_member_account(account) -> bool:
    """Правление/бухгалтер/председатель видят все лицевые счета; рядовой член — только свои."""
    if is_board():
        return True
    return g.user.person_id is not None and g.user.person_id == account.person_id


def sync_user_role(person) -> None:
    """
    Синхронизирует роль привязанной к человеку учётной записи (User.role) с
    его флагами управления (is_chairman/is_accountant/is_board_member).
    Без этого чекбоксы/членство в созыве были бы чисто информационными и не
    влияли бы на реальные права входа. Приоритет при нескольких флагах:
    председатель > бухгалтер > правление. Используется и при ручной правке
    флагов на карточке человека (persons.py), и при формировании состава
    созыва правления (governance.py) — единая точка синхронизации.
    """
    from . import database
    from .models import User

    user = database.db_session.query(User).filter_by(person_id=person.id).first()
    if user is None:
        return
    old_role = user.role
    if person.is_chairman:
        user.role = RoleEnum.CHAIRMAN
    elif person.is_accountant:
        user.role = RoleEnum.ACCOUNTANT
    elif person.is_board_member:
        user.role = RoleEnum.BOARD
    else:
        user.role = RoleEnum.MEMBER

    if user.role != old_role:
        from . import audit
        audit.record(
            "role.change", entity_type="user", entity_id=user.id,
            summary=f"Роль «{user.username}» ({person.short_name}) изменена: {old_role.value} → {user.role.value}",
        )
