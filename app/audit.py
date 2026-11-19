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

Повторные правки одной сущности схлопываются (см. _coalesce_target): если
тот же пользователь снова правит ту же страницу/гараж/… в пределах
COALESCE_WINDOW после своей же правки (или создания), новая запись не
добавляется — у последней лишь сдвигается время. Иначе журнал зарастает
«Изменена страница … / Изменена страница …» на каждое сохранение. Касается
только *.edit-действий с указанной сущностью — деньги, роли, вход и т.п.
(create/delete/прочее) всегда пишутся отдельными записями.
"""
import datetime as dt
import re
from decimal import Decimal

from flask import g, url_for

from . import database
from .models import AuditLog, ForumPost, WikiPage


def record(action: str, summary: str, entity_type: str | None = None, entity_id: int | None = None, actor=None,
           notify_exclude_actor: bool = True) -> None:
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

    Заодно ставит уведомление правлению (notifications.queue_board_event) —
    уйдёт только после commit этой же транзакции. Автора действия не
    уведомляем; notify_exclude_actor=False — для событий, у которых «автор»
    случайный (решение по предложению подводится при чьём-то просмотре
    страницы, см. proposals.resolve_if_due).
    """
    user = actor if actor is not None else g.get("user", None)
    previous = _coalesce_target(action, entity_type, entity_id, user)
    if previous is not None:
        if previous.action == action:
            previous.summary = summary  # заголовок могли переименовать
        previous.created_at = dt.datetime.utcnow()
        return  # и без повторного уведомления правлению
    database.db_session.add(AuditLog(
        actor_user_id=user.id if user else None,
        actor_username=user.username if user else None,
        actor_role=user.role.value if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
    ))
    from . import notifications
    notifications.queue_board_event(
        action, summary, entity_type, entity_id,
        actor_user_id=user.id if user and notify_exclude_actor else None,
        actor_name=_actor_name(user) if user and notify_exclude_actor else None,
    )


COALESCE_WINDOW = dt.timedelta(days=1)


def _is_edit_action(action: str) -> bool:
    return action.endswith(".edit") or action.endswith("_edit")


def _coalesce_target(action: str, entity_type: str | None, entity_id: int | None, user) -> AuditLog | None:
    """Запись, в которую можно влить эту правку: последняя по той же
    сущности (кем бы ни сделана), если она от того же пользователя, не
    старше COALESCE_WINDOW и это та же правка или создание этой сущности
    (wiki.create → wiki.edit, power.reading_add → power.reading_edit).
    Чужая запись между правками разрывает цепочку."""
    if not _is_edit_action(action) or entity_type is None or entity_id is None or user is None:
        return None
    last = (
        database.db_session.query(AuditLog)
        .filter(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )
    if last is None or last.actor_user_id != user.id:
        return None
    if dt.datetime.utcnow() - last.created_at > COALESCE_WINDOW:
        return None
    stem = action[:-len("edit")]
    if last.action not in (action, stem + "create", stem + "add"):
        return None
    return last


def _actor_name(user) -> str:
    return user.person.full_name if user.person else user.username


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
    if entity_type == "wiki":
        page = database.db_session.get(WikiPage, entity_id)
        return url_for("wiki.view", slug=page.slug) if page else None
    if entity_type == "proposal":
        return url_for("proposals.detail", proposal_id=entity_id)
    if entity_type == "application":
        return url_for("applications.detail", application_id=entity_id)
    if entity_type == "certificate_request":
        return url_for("documents.index")
    if entity_type == "issued_document":
        return url_for("documents.detail", document_id=entity_id)
    if entity_type == "news":
        return url_for("news.view", news_id=entity_id)
    if entity_type == "forum_topic":
        return url_for("forum.view", topic_id=entity_id)
    if entity_type == "forum_post":
        post = database.db_session.get(ForumPost, entity_id)
        return url_for("forum.view", topic_id=post.topic_id, _anchor=f"post-{post.id}") if post else None
    if entity_type == "bulletin_post":
        return url_for("bulletin.list_posts", _anchor=f"bulletin-{entity_id}")
    if entity_type == "mailbox_trash_message":
        # UID письма в «Корзине» почты правления (app/mailbox.py: delete_message).
        from .models import MailboxSettings
        settings = database.db_session.query(MailboxSettings).first()
        if settings is None or not settings.trash_folder:
            return None
        return url_for("mailbox.view_message", uid=entity_id, folder=settings.trash_folder)
    return None


# Записи без своей карточки (настройки, удалённые объекты, массовые
# действия) — ссылка на раздел, к которому относится действие: точное
# совпадение кода, иначе префикс «раздел.». Порядок не важен — точный
# код проверяется раньше префикса.
_ACTION_PAGES = {
    "account_format.": ("finance.account_format", {}),
    "bank_account.": ("finance.bank_accounts", {}),
    "board_term.": ("governance.view", {}),
    "revision_commission.": ("governance.view", {}),
    "candidacy.": ("governance.view", {}),
    "control_meter.": ("control_meters.list_tree", {}),
    "cooperative.": ("cooperative.view", {}),
    "counterparty.": ("counterparties.list_counterparties", {}),
    "document.": ("cooperative.list_documents", {}),
    "external_login.": ("external_login.settings_page", {}),
    "facsimile.": ("cooperative.edit", {}),
    "fee_type.": ("finance.fee_types", {}),
    "key_rate.": ("penalty.view", {}),
    "penalty_amnesty.": ("penalty.view", {}),
    "mailbox.": ("mailbox.inbox", {}),
    "max.": ("max_settings.view", {}),
    "telegram.": ("telegram_settings.view", {}),
    "sms.": ("sms_settings.view", {}),
    "meeting.": ("meetings.list_meetings", {}),
    "member_account.": ("finance.member_accounts", {}),
    "news.": ("main.index", {}),
    "news_gallery.": ("news_gallery.index", {}),
    "power.": ("power.view", {}),
    "vote.": ("voting.list_votes", {}),
    "wiki.": ("wiki.list_pages", {}),
    "postal_api.": ("postal_letters.api_settings", {}),
}

# Действия над счётом банка — на ту страницу счёта, где это видно.
_BANK_ACCOUNT_PAGES = (
    ("bank_api.statement", "bank_sync.statement"),
    ("bank_api.balance", "bank_sync.statement"),
    ("bank_api.payment_registry", "bank_sync.payment_registry"),
    ("bank_api.charge_registry", "bank_sync.charge_registry"),
)


# Период выписки из текста записи: «за ДД.ММ.ГГГГ—ДД.ММ.ГГГГ» (загрузка
# выписки) или одна дата «от ДД.ММ.ГГГГ» (операция выписки). Период в
# запись отдельным полем не пишется, а без него ссылка открывала выписку
# за последние 30 дней — нужной загрузки или операции там могло не быть.
_STATEMENT_RANGE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*[—–-]\s*(\d{2}\.\d{2}\.\d{4})")
_STATEMENT_DAY_RE = re.compile(r"\bот (\d{2}\.\d{2}\.\d{4})")


def _statement_period(summary: str | None) -> dict:
    if not summary:
        return {}
    parse = lambda value: dt.datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    try:
        if match := _STATEMENT_RANGE_RE.search(summary):
            return {"date_from": parse(match.group(1)), "date_to": parse(match.group(2))}
        if match := _STATEMENT_DAY_RE.search(summary):
            day = parse(match.group(1))
            return {"date_from": day, "date_to": day}
    except ValueError:
        pass
    return {}


def _entity_link(action: str | None, entity_type: str | None, entity_id: int | None,
                 summary: str | None = None) -> str | None:
    """Ссылки на сущности, которых нет в entity_url: им нужен код действия
    (счёт банка — выписка или реестр) или запрос в БД."""
    if entity_id is None:
        return None
    from .models import Document, PostalDispatch, CounterpartyPayment, User
    if entity_type == "bank_account":
        for prefix, endpoint in _BANK_ACCOUNT_PAGES:
            if action and action.startswith(prefix):
                period = _statement_period(summary) if endpoint == "bank_sync.statement" else {}
                return url_for(endpoint, account_id=entity_id, **period)
        return url_for("finance.bank_accounts")
    if entity_type == "user":
        user = database.db_session.get(User, entity_id)
        if user is not None and user.person_id:
            return url_for("persons.detail", person_id=user.person_id)
        return url_for("persons.accounts_list")
    if entity_type == "document":
        doc = database.db_session.get(Document, entity_id)
        if doc is not None and doc.counterparty_id:
            return url_for("counterparties.detail", counterparty_id=doc.counterparty_id)
        return url_for("cooperative.list_documents")
    if entity_type == "postal_dispatch":
        return url_for("postal_letters.edit", dispatch_id=entity_id) if database.db_session.get(PostalDispatch, entity_id) else None
    if entity_type == "counterparty_payment":
        payment = database.db_session.get(CounterpartyPayment, entity_id)
        return url_for("counterparties.detail", counterparty_id=payment.counterparty_id) if payment else None
    if entity_type == "control_meter":
        return url_for("control_meters.detail", node_id=entity_id)
    if entity_type == "court_section":
        return url_for("legal_docs.court_sections_list")
    if entity_type == "annual_report":
        return url_for("annual_reports.view", report_id=entity_id)
    if entity_type == "fee_type":
        return url_for("finance.fee_types")
    if entity_type == "cooperative":
        return url_for("cooperative.view")
    if entity_type == "postal_api_settings":
        return url_for("postal_letters.api_settings")
    return None


def link_for(action: str | None, entity_type: str | None, entity_id: int | None,
             summary: str | None = None) -> str | None:
    """Ссылка из записи журнала: на карточку сущности, иначе — на раздел,
    к которому относится действие (_ACTION_PAGES). summary нужен выписке
    счёта банка — из него берётся период (_statement_period)."""
    try:
        link = entity_url(entity_type, entity_id) or _entity_link(action, entity_type, entity_id, summary)
    except Exception:
        link = None  # удалённая сущность/маршрут — без ссылки, журнал не должен падать
    if link or not action:
        return link
    for prefix, (endpoint, kwargs) in _ACTION_PAGES.items():
        if action == prefix or action.startswith(prefix):
            return url_for(endpoint, **kwargs)
    return None

