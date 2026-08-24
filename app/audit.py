"""
Журнал аудита — кто и что сделал с деньгами/доступом кооператива.

record() только добавляет запись в сессию (flush, без commit) — вызывающий
код коммитит её вместе с самим изменением в той же транзакции, тем же
образом, как ChargeAllocation добавляется вместе с Charge/Payment. Если
транзакция откатится, запись в журнале тоже не сохранится — это осознанно:
хотим согласованность "запись есть <=> действие реально применилось",
а не "может залогировано, а может и нет, независимо от исхода".

Не логируем сам факт вызова record() через flash/логгер — это внутренний
механизм, отдельный от пользовательских уведомлений.
"""
from flask import g

from . import database
from .models import AuditLog


def record(action: str, summary: str, entity_type: str | None = None, entity_id: int | None = None, actor=None) -> None:
    """
    action — короткий машиночитаемый код, напр. "payment.create", "charge.create",
    "role.change", "auth.login", "auth.login_failed", "account.password_change".
    summary — готовая человекочитаемая строка на русском для показа в журнале
    (не переводится через i18n — журнал аудита ведётся на языке кооператива,
    как и остальные юридически значимые документы вроде PD-4).
    actor — обычно None (берём g.user); передаётся явно только там, где
    действие меняет самого текущего пользователя в той же обработке запроса
    (например login() — g.user ещё не обновлён в момент успешного входа,
    т.к. load_logged_in_user() уже отработал до этого запроса).
    """
    user = actor if actor is not None else g.get("user", None)
    database.db_session.add(AuditLog(
        actor_user_id=user.id if user else None,
        actor_username=user.username if user else None,
        actor_role=user.role.value if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
    ))
