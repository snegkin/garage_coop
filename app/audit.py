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
from decimal import Decimal

from flask import g, url_for

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


def format_amount(value) -> str:
    """
    Сумма для текста записи журнала — всегда в фиксированном русском
    формате (запятая, ₽), НЕ через i18n.fmt2 (которая берёт разделитель из
    g.locale текущего пользователя): текст журнала не должен меняться от
    того, в каком языковом режиме интерфейса действовал автор записи или
    сейчас читает журнал смотрящий (см. docstring record() выше — журнал
    всегда на языке кооператива).
    """
    quantized = Decimal(value).quantize(Decimal("0.01"))
    return f"{quantized:.2f}".replace(".", ",") + " ₽"


def format_date(value) -> str:
    """Дата для текста записи журнала — всегда ДД.ММ.ГГГГ, по той же
    причине, что и format_amount выше (не format_date из i18n.py)."""
    return value.strftime("%d.%m.%Y")


def entity_url(entity_type: str | None, entity_id: int | None) -> str | None:
    """
    Ссылка на карточку сущности записи журнала (см.
    governance/audit_log.html) — строится здесь, при отображении, а не
    хранится в самой записи: так переживает переименование/переезд
    маршрутов, случившиеся уже после того, как запись была сделана.
    Только для типов, у которых вообще есть отдельная карточка, куда имеет
    смысл сослаться из журнала.
    """
    if entity_id is None:
        return None
    if entity_type == "person":
        return url_for("persons.detail", person_id=entity_id)
    if entity_type == "counterparty":
        return url_for("counterparties.detail", counterparty_id=entity_id)
    if entity_type == "member_account":
        return url_for("finance.member_account_detail", account_id=entity_id)
    if entity_type == "vote":
        return url_for("voting.detail", vote_id=entity_id)
    if entity_type == "garage":
        return url_for("garages.detail", garage_id=entity_id)
    return None
